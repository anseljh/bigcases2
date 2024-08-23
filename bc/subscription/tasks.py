from datetime import date, datetime
from typing import Literal

from django.conf import settings
from django.db import transaction
from django_rq.queues import Queue, get_queue
from rq import Retry

from bc.channel.models import Channel, Post
from bc.channel.selectors import (
    get_channels_per_subscription,
    get_sponsored_groups_per_subscription,
)
from bc.core.utils.images import add_sponsored_text_to_thumbnails
from bc.core.utils.microservices import get_thumbnails_from_range
from bc.core.utils.status.selectors import (
    get_new_case_template,
    get_template_for_channel,
)
from bc.core.utils.status.templates import DO_NOT_PAY, DO_NOT_POST
from bc.sponsorship.selectors import check_active_sponsorships
from bc.sponsorship.services import log_purchase
from bc.subscription.utils.courtlistener import (
    DocketDict,
    DocumentDict,
    download_pdf_from_cl,
    is_bankruptcy,
    lookup_docket_by_cl_id,
    lookup_document_by_doc_id,
    lookup_initial_complaint,
    purchase_pdf_by_doc_id,
)

from .models import FilingWebhookEvent, Subscription
from .types import Document

queue: Queue = get_queue("default")


def enqueue_posts_for_new_case(
    subscription: Subscription,
    document_url: str | None = None,
    check_sponsor_message: bool = False,
    initial_document: DocumentDict | None = None,
) -> None:
    """
    Enqueue jobs to create a post in the available channels after
    following a new case.

    Args:
        subscription (Subscription): the new subscription object.
        document_url (str | None): URL path to download the docuement.
        check_sponsor_message (bool, optional): designates whether this method
            should check the sponsorships field and compute the sponsor_message
            for each channel. Defaults to False.
    """

    files = None
    if document_url:
        document = download_pdf_from_cl(document_url)
        files = get_thumbnails_from_range(document, "[1,2,3,4]")

    docket: DocketDict | None = None
    date_filed: date
    days_old = 0

    if subscription.cl_docket_id:
        if initial_document is None:
            initial_document = lookup_initial_complaint(
                subscription.cl_docket_id
            )
        docket = lookup_docket_by_cl_id(subscription.cl_docket_id)

        date_filed_str = docket["date_filed"] if docket else None
        if date_filed_str:
            try:
                date_filed = datetime.strptime(
                    date_filed_str, "%Y-%m-%d"
                ).date()

                assert date_filed is not None  # For type checking purposes
                days_old = (date.today() - date_filed).days
            except ValueError:
                # If the date_filed_str is not a valid date, just continue on
                pass

    initial_complaint_link = (
        f"https://www.courtlistener.com{initial_document['absolute_url']}"
        if initial_document
        else None
    )

    initial_complaint_type: Literal["Petition", "Complaint"] = (
        "Petition" if is_bankruptcy(subscription.cl_court_id) else "Complaint"
    )

    for channel in get_channels_per_subscription(subscription.pk):
        template = get_new_case_template(channel.service)

        if channel.group:
            template.border_color = channel.group.border_color_rgb

        message, _ = template.format(
            docket=subscription.name_with_summary,
            docket_link=subscription.cl_url,
            docket_id=subscription.cl_docket_id,
            article_url=subscription.article_url,
            date_filed=date_filed if days_old >= 30 else None,
            initial_complaint_type=initial_complaint_type,
            initial_complaint_link=initial_complaint_link,
        )

        api = channel.get_api_wrapper()

        sponsor_message = None
        sponsorships_for_channel = channel.group.sponsorships.all()  # type: ignore
        if check_sponsor_message and sponsorships_for_channel and files:
            sponsorship = sponsorships_for_channel[0]
            sponsor_message = sponsorship.watermark_message
            files = add_sponsored_text_to_thumbnails(files, sponsor_message)

        queue.enqueue(
            api.add_status,
            message,
            None,
            files,
            retry=Retry(
                max=settings.RQ_MAX_NUMBER_OF_RETRIES,
                interval=settings.RQ_POST_RETRY_INTERVALS,
            ),
        )


def enqueue_posts_for_docket_alert(
    webhook_event: FilingWebhookEvent,
    document_url: str | None = None,
    check_sponsor_message: bool = False,
) -> None:
    """
    Enqueue jobs to create a post in the available channels after
    handling a docket alert webhook.

    Args:
        webhook_event (FilingWebhookEvent): The FilingWebhookEvent record.
        document_url (str | None): URL to download the document.
        check_sponsor_message (bool, optional): designates whether this method
            should check. the sponsorships field and compute the sponsor_message
            for each channel. Defaults to False.
    """
    if not webhook_event.subscription:
        return

    for channel in get_channels_per_subscription(
        webhook_event.subscription.pk
    ):
        sponsor_message = None
        sponsorships_for_channel = channel.group.sponsorships.all()  # type: ignore
        if check_sponsor_message and sponsorships_for_channel:
            sponsorship = sponsorships_for_channel[0]
            sponsor_message = sponsorship.watermark_message

        queue.enqueue(
            make_post_for_webhook_event,
            channel.pk,
            webhook_event.pk,
            document_url,
            sponsor_message,
            retry=Retry(
                max=settings.RQ_MAX_NUMBER_OF_RETRIES,
                interval=settings.RQ_POST_RETRY_INTERVALS,
            ),
        )


@transaction.atomic
def process_filing_webhook_event(fwe_pk: int) -> FilingWebhookEvent:
    """Process an event from a CL webhook.

    This function links a webhook event to one of the records in the
    subscription table or ignores it if the bot is not following the
    case.

    :param fwe_pk: The PK of the FilingWebhookEvent record.
    :return: The FilingWebhookEvent object that was updated.
    """
    filing_webhook_event = FilingWebhookEvent.objects.get(pk=fwe_pk)

    if not filing_webhook_event.docket_id:
        return filing_webhook_event

    try:
        with transaction.atomic():
            subscription = Subscription.objects.get(
                cl_docket_id=filing_webhook_event.docket_id
            )
    except Subscription.DoesNotExist:
        # We don't know why we got this webhook event. Ignore it.
        filing_webhook_event.status = FilingWebhookEvent.FAILED
        filing_webhook_event.save()
        return filing_webhook_event

    filing_webhook_event.subscription = subscription
    filing_webhook_event.status = FilingWebhookEvent.SUCCESSFUL
    filing_webhook_event.save()

    return filing_webhook_event


@transaction.atomic
def check_webhook_before_posting(fwe_pk: int):
    """Checks the webhook event before start posting

    This function checks the description of the event to avoid
    creating post for junk docket entries, also checks if the document
    associated with the webhook is available in the RECAP archive to
    retrieve it and use it to create a post in the enabled channels.

    :param fwe_pk: The PK of the FilingWebhookEvent record.
    :return: the FilingWebhookEvent object used to .
    """
    filing_webhook_event = FilingWebhookEvent.objects.get(pk=fwe_pk)

    if filing_webhook_event.status != FilingWebhookEvent.SUCCESSFUL:
        return filing_webhook_event

    # check if the webhook event is linked to a subscription record
    if not filing_webhook_event.subscription:
        raise AssertionError(
            "The webhook event doesn't have a relationship with a subscription record"
        )

    # check the description to filter junk docket entries
    if DO_NOT_POST.search(filing_webhook_event.description):
        filing_webhook_event.status = FilingWebhookEvent.IGNORED
        filing_webhook_event.save(update_fields=["status"])
        return filing_webhook_event

    # check if the document is available or there's a sponsorship to purchase it.
    document_url = None
    cl_document = lookup_document_by_doc_id(filing_webhook_event.doc_id)
    if cl_document["filepath_local"]:
        document_url = cl_document["filepath_local"]
    else:
        sponsorship = check_active_sponsorships(
            filing_webhook_event.subscription.pk
        )
        if (
            sponsorship
            and filing_webhook_event.pacer_doc_id
            and not DO_NOT_PAY.search(filing_webhook_event.description)
        ):
            purchase_pdf_by_doc_id(
                filing_webhook_event.doc_id, filing_webhook_event.docket_id
            )
            filing_webhook_event.status = (
                FilingWebhookEvent.WAITING_FOR_DOCUMENT
            )
            filing_webhook_event.save(update_fields=["status"])
            return filing_webhook_event

    # Got the document or no sponsorship. Tweet and toot.
    enqueue_posts_for_docket_alert(filing_webhook_event, document_url)

    return filing_webhook_event


@transaction.atomic
def check_initial_complaint_before_posting(
    subscription_pk: int,
) -> Subscription:
    """Checks whether the initial complaint of the case is available
    in the RECAP archive or not to retrieve it and use it to create a
    post in the enabled channels.

    This method also checks the active sponsorships when the initial
    complaint is not available in the archive. The file is purchased if
    there's a sponsorship available for the subscription.

    :param subscription_pk: The PK of the subscription record.
    :return: the subscription object used to create the posts.
    """
    subscription = Subscription.objects.get(pk=subscription_pk)

    document_url = None
    cl_document = lookup_initial_complaint(subscription.cl_docket_id)
    if cl_document and cl_document["filepath_local"]:
        document_url = cl_document["filepath_local"]
    elif cl_document and cl_document["pacer_doc_id"]:
        sponsorship = check_active_sponsorships(subscription.pk)
        if sponsorship:
            purchase_pdf_by_doc_id(
                cl_document["id"], subscription.cl_docket_id
            )
            return subscription

    # Got the document or no sponsorship. Tweet and toot.
    enqueue_posts_for_new_case(
        subscription, document_url, initial_document=cl_document
    )

    return subscription


@transaction.atomic
def process_fetch_webhook_event(
    record_pk: int,
    record_type: Literal["filing_webhook", "subscription"] = "filing_webhook",
) -> int:
    """Process a RECAP fetch webhook event from CL.

    This functions retrieves the new document available in the
    RECAP archive, creates a new entry related to the purchase
    in the ledger and schedule the tasks to create new post in
    the enabled channels.

    :param record_pk: The PK of the of record that triggered the purchase.
    :param record_type: The type of record that triggered the purchase.
    :return: The PK of the of record that triggered the purchase.
    """
    cl_document: DocumentDict | None
    if record_type == "filing_webhook":
        filing_webhook_event = FilingWebhookEvent.objects.get(pk=record_pk)

        # check if the webhook event is linked to a subscription record
        if not filing_webhook_event.subscription:
            raise AssertionError(
                "The webhook event doesn't have a relationship with a subscription record"
            )

        filing_webhook_event.status = FilingWebhookEvent.SUCCESSFUL
        filing_webhook_event.save(update_fields=["status"])

        subscription = filing_webhook_event.subscription
        cl_document = lookup_document_by_doc_id(filing_webhook_event.doc_id)
    else:
        subscription = Subscription.objects.get(pk=record_pk)
        cl_document = lookup_initial_complaint(subscription.cl_docket_id)

    if not cl_document:
        raise AssertionError("The RECAP document lookup failed")

    if not cl_document["filepath_local"]:
        raise AssertionError(
            "The RECAP document doesn't have a path to download the file"
        )

    pdf_path = cl_document["filepath_local"]

    sponsor_groups = get_sponsored_groups_per_subscription(subscription.pk)

    document = Document(
        description=(
            f"Initial Complaint from {subscription.docket_name}"
            if record_type == "subscription"
            else str(filing_webhook_event)
        ),
        page_count=cl_document["page_count"],
        docket_number=subscription.docket_number,
        court_name=subscription.court_name,
        court_id=subscription.pacer_court_id,
    )
    if sponsor_groups:
        log_purchase(sponsor_groups, subscription.pk, document)

    if record_type == "filing_webhook":
        enqueue_posts_for_docket_alert(filing_webhook_event, pdf_path, True)
    else:
        enqueue_posts_for_new_case(subscription, pdf_path, True, cl_document)

    return record_pk


@transaction.atomic
def make_post_for_webhook_event(
    channel_pk: int,
    fwe_pk: int,
    document_url: str | None,
    sponsor_text: str | None = None,
) -> Post:
    """Post a new status in the given channel using the data of the given webhook
    event and subscription.

    Args:
        channel_pk (int): The pk of the channel where the post will be created.
        fwe_pk (int): The PK of the FilingWebhookEvent record.
        document_url (str | None): URL path to download the document.
        sponsor_text (str | None): sponsor message to include in the thumbnails.

    Returns:
        Post: A post object with the data of the new status that was created
    """

    channel = Channel.objects.get(pk=channel_pk)
    filing_webhook_event = FilingWebhookEvent.objects.get(pk=fwe_pk)

    if not filing_webhook_event.subscription:
        raise AssertionError(
            "The webhook event doesn't have a relationship with a subscription record"
        )

    template = get_template_for_channel(
        channel.service, filing_webhook_event.document_number
    )

    if channel.group:
        template.border_color = channel.group.border_color_rgb

    message, image = template.format(
        docket=filing_webhook_event.subscription.name_with_summary,
        description=filing_webhook_event.description,
        doc_num=filing_webhook_event.document_number_with_attachment,
        pdf_link=filing_webhook_event.cl_pdf_or_pacer_url,
        docket_link=filing_webhook_event.cl_docket_url,
        docket_id=filing_webhook_event.docket_id,
    )

    files = None
    if document_url:
        document = download_pdf_from_cl(document_url)
        thumbnail_range = "[1,2,3]" if image else "[1,2,3,4]"
        files = get_thumbnails_from_range(document, thumbnail_range)

    if sponsor_text and files:
        files = add_sponsored_text_to_thumbnails(files, sponsor_text)

    api = channel.get_api_wrapper()
    api_post_id = api.add_status(message, image, files)

    return Post.objects.create(
        filing_webhook_event=filing_webhook_event,
        channel=channel,
        object_id=api_post_id,
        text=message,
    )
