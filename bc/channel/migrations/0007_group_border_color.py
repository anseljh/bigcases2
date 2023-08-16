# Generated by Django 4.2.3 on 2023-08-16 11:36

from django.db import migrations
import spectrum.fields


class Migration(migrations.Migration):
    dependencies = [
        ("channel", "0006_group_overview_group_slug"),
    ]

    operations = [
        migrations.AddField(
            model_name="group",
            name="border_color",
            field=spectrum.fields.ColorField(
                default="#F3C33E",
                help_text="Color used in the images' borders of this group",
            ),
        ),
    ]
