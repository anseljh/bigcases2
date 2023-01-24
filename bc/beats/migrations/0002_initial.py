# Generated by Django 4.1.5 on 2023-01-20 15:48

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("channels", "0001_initial"),
        ("beats", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("cases", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="beat",
            name="channels",
            field=models.ManyToManyField(
                help_text="Foreign key as a relation to the Channel object.",
                related_name="beats",
                to="channels.channel",
            ),
        ),
        migrations.AddField(
            model_name="beat",
            name="curators",
            field=models.ManyToManyField(
                help_text="Foreign key as a relation to the User object.",
                related_name="beats",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="beat",
            name="docket",
            field=models.ManyToManyField(
                help_text="Foreign key as a relation to the Docket object.",
                related_name="beats",
                to="cases.docket",
            ),
        ),
    ]