from django.conf import settings
from django.db import models

class ReplacementSupplyPlan(models.Model):
    id=models.BigAutoField(primary_key=True)
    factory=models.ForeignKey('account.Organization',on_delete=models.PROTECT,related_name='+')
    merchant=models.ForeignKey('account.Organization',on_delete=models.PROTECT,related_name='+')
    normal_order=models.OneToOneField('sales.OrderPlan',null=True,blank=True,on_delete=models.PROTECT,related_name='replacement_supply_plan')
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    client_request_id=models.CharField(max_length=64)
    status=models.CharField(max_length=16,default='OPEN')
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['created_by','client_request_id'],name='replacement_supply_request_unique')]

class ReplacementSupplyPlanItem(models.Model):
    id=models.BigAutoField(primary_key=True)
    plan=models.ForeignKey(ReplacementSupplyPlan,on_delete=models.PROTECT,related_name='items')
    leg=models.ForeignKey('lifecycle.ReplacementLeg',on_delete=models.PROTECT,related_name='supply_plan_items')
    status=models.CharField(max_length=16,default='RESERVED')
    class Meta:
        constraints=[models.UniqueConstraint(fields=['leg'],condition=models.Q(status__in=['RESERVED','SENT']),name='replacement_supply_active_leg_unique')]
