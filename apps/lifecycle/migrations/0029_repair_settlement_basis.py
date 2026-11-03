from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [("lifecycle", "0028_after_sales_station_triage_materials")]
    operations = [migrations.AddField(
        model_name="aftersalesapplication", name="repair_settlement_basis",
        field=models.JSONField(default=dict, blank=True),
    )]
