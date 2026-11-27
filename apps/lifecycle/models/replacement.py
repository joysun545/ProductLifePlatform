"""Non-financial replacement facts. Customer replacement and channel recovery are independent."""
from django.conf import settings
from django.db import models

class ReplacementFlow(models.Model):
    application = models.OneToOneField('lifecycle.AfterSalesApplication',on_delete=models.PROTECT,related_name='replacement_flow')
    protocol_version = models.PositiveSmallIntegerField(default=5)
    service_mode = models.CharField(max_length=8,blank=True,default='')
    service_confirmed_at = models.DateTimeField(null=True,blank=True)
    recovery = models.JSONField(default=dict)
    mode = models.CharField(max_length=16,choices=[('LOCAL','组织库存换新'),('FACTORY','厂家直接换新')])
    execution_organization = models.ForeignKey('account.Organization',on_delete=models.PROTECT)
    chain = models.JSONField(default=list)  # frozen factory -> ... -> execution organization
    activation_method = models.CharField(max_length=24)
    first_activation_at = models.DateTimeField()
    activation_snapshot = models.JSONField(default=dict)
    swapped_at = models.DateTimeField(null=True,blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

class ReplacementLeg(models.Model):
    flow = models.ForeignKey(ReplacementFlow,on_delete=models.PROTECT,related_name='legs')
    direction = models.CharField(max_length=16,choices=[('OLD','旧机返厂'),('SUPPLY','换新补发'),('USER','新机交付用户')])
    sequence = models.PositiveIntegerField()
    from_organization = models.ForeignKey('account.Organization',on_delete=models.PROTECT,related_name='+')
    to_organization = models.ForeignKey('account.Organization',on_delete=models.PROTECT,related_name='+')
    device = models.ForeignKey('lifecycle.Device',null=True,blank=True,on_delete=models.PROTECT)
    status = models.CharField(max_length=16,default='PENDING',choices=[('PENDING','待发出'),('SENT','已发出待签收'),('RECEIVED','已签收')],db_index=True)
    shipment_id = models.PositiveBigIntegerField(null=True,blank=True)  # validated normal-shipment carrier link, never a sales item
    logistics = models.JSONField(default=dict)
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL,null=True,on_delete=models.PROTECT,related_name='+')
    sent_at = models.DateTimeField(null=True,blank=True)
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL,null=True,on_delete=models.PROTECT,related_name='+')
    received_at = models.DateTimeField(null=True,blank=True)
    receipt_note = models.TextField(blank=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['flow','direction','sequence'],name='replacement_unique_leg')]
        ordering=['direction','sequence']

class ReplacementInventoryEntry(models.Model):
    flow = models.ForeignKey(ReplacementFlow,on_delete=models.PROTECT,related_name='inventory_entries')
    leg = models.ForeignKey(ReplacementLeg,on_delete=models.PROTECT)
    organization = models.ForeignKey('account.Organization',on_delete=models.PROTECT)
    device = models.ForeignKey('lifecycle.Device',on_delete=models.PROTECT)
    kind = models.CharField(max_length=24) # LOCAL_OUT, SUPPLY_IN, SUPPLY_OUT, OLD_IN, OLD_OUT; never financial
    quantity = models.SmallIntegerField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['leg','organization','kind'],name='replacement_unique_stock_fact')]

class ReplacementTurnoverStock(models.Model):
    id = models.BigAutoField(primary_key=True)
    flow = models.OneToOneField(ReplacementFlow,on_delete=models.PROTECT,related_name='turnover_stock')
    device = models.OneToOneField('lifecycle.Device',on_delete=models.PROTECT,related_name='replacement_turnover_stock')
    factory = models.ForeignKey('account.Organization',on_delete=models.PROTECT)
    status = models.CharField(max_length=16,default='AVAILABLE')
    stored_by = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    stored_at = models.DateTimeField(auto_now_add=True)

class ReplacementMaterialReturn(models.Model):
    id = models.BigAutoField(primary_key=True)
    flow = models.ForeignKey(ReplacementFlow,on_delete=models.PROTECT,related_name='material_returns')
    bom_item = models.ForeignKey('product.BOMItem',on_delete=models.PROTECT)
    supplier = models.ForeignKey('account.Organization',on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=14,decimal_places=3)
    status = models.CharField(max_length=16,default='NOTIFIED')
    snapshot = models.JSONField(default=dict)
    logistics = models.JSONField(default=dict)
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL,null=True,on_delete=models.PROTECT,related_name='+')
    sent_at = models.DateTimeField(null=True)
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL,null=True,on_delete=models.PROTECT,related_name='+')
    received_at = models.DateTimeField(null=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['flow','bom_item'],name='replacement_unique_return_part'),
                     models.CheckConstraint(condition=models.Q(quantity__gt=0),name='replacement_return_quantity_positive')]
