from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("product", "0006_productdocument_file_fields"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="productmodel",
            name="brand",
        ),
    ]
