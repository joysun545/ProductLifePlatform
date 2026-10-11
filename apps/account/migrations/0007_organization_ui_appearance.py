from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("account", "0006_factory_warehouse_roles"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="ui_color_scheme",
            field=models.CharField(
                blank=True,
                choices=[
                    ("jade", "翡翠绿"),
                    ("ocean", "深海蓝"),
                    ("amber", "琥珀金"),
                ],
                default="",
                help_text="仅系统运营业主设置；留空时使用翡翠绿。",
                max_length=16,
                verbose_name="小程序全局色系",
            ),
        ),
        migrations.AddField(
            model_name="organization",
            name="ui_style_scheme",
            field=models.CharField(
                blank=True,
                choices=[
                    ("flow", "柔和流线"),
                    ("order", "专业秩序"),
                    ("layer", "层叠质感"),
                ],
                default="",
                help_text="仅系统运营业主设置；留空时使用柔和流线。",
                max_length=16,
                verbose_name="小程序全局页面风格",
            ),
        ),
        migrations.AddConstraint(
            model_name="organization",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(("ui_color_scheme", ""))
                    | models.Q(("ui_color_scheme__in", ["jade", "ocean", "amber"]))
                ),
                name="account_organization_valid_ui_color",
            ),
        ),
        migrations.AddConstraint(
            model_name="organization",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(("ui_style_scheme", ""))
                    | models.Q(("ui_style_scheme__in", ["flow", "order", "layer"]))
                ),
                name="account_organization_valid_ui_style",
            ),
        ),
    ]
