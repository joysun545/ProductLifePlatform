from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("product", "0005_productdocument"),
    ]

    operations = [
        migrations.AlterField(
            model_name="productdocument",
            name="content",
            field=models.TextField(blank=True, default="", verbose_name="正文"),
        ),
        migrations.AddField(
            model_name="productdocument",
            name="document_file",
            field=models.FileField(
                blank=True,
                upload_to="product/documents/%Y/%m/",
                verbose_name="原始文档",
            ),
        ),
        migrations.AddField(
            model_name="productdocument",
            name="original_filename",
            field=models.CharField(
                blank=True,
                default="",
                max_length=255,
                verbose_name="原始文件名",
            ),
        ),
        migrations.AddField(
            model_name="productdocument",
            name="file_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("TEXT", "TXT文本"),
                    ("MARKDOWN", "Markdown文本"),
                    ("DOCX", "Word文档"),
                    ("PDF", "PDF文档"),
                ],
                default="",
                max_length=16,
                verbose_name="文件类型",
            ),
        ),
        migrations.AddField(
            model_name="productdocument",
            name="mime_type",
            field=models.CharField(
                blank=True,
                default="",
                max_length=128,
                verbose_name="媒体类型",
            ),
        ),
        migrations.AddField(
            model_name="productdocument",
            name="file_size",
            field=models.PositiveBigIntegerField(default=0, verbose_name="文件大小"),
        ),
    ]
