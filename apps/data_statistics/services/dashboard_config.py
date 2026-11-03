ROLE_MODULES = {
    "customer_owner": ("AFTERSALES",),
    "driver": ("AFTERSALES",),
    "factory_admin": ("FLOW", "MATERIAL", "AFTERSALES", "QUALITY"),
    "factory_owner": ("FLOW", "MATERIAL", "AFTERSALES", "QUALITY"),
    "factory_senior_manager": ("FLOW", "MATERIAL", "AFTERSALES", "QUALITY"),
    "factory_manager": ("FLOW", "MATERIAL", "AFTERSALES", "QUALITY"),
    "factory_chief_engineer": ("AFTERSALES", "MATERIAL", "QUALITY"),
    "factory_sales": ("FLOW", "AFTERSALES", "QUALITY"),
    "factory_sales_assistant": ("FLOW", "QUALITY"),
    "factory_production": ("FLOW", "MATERIAL", "AFTERSALES"),
    "factory_matching": ("MATERIAL", "AFTERSALES", "QUALITY"),
    "factory_purchase": ("MATERIAL", "AFTERSALES"),
    "factory_stock": ("FLOW", "QUALITY"),
    "factory_material_stock": ("MATERIAL",),
    "factory_logistics": ("FLOW", "QUALITY"),
    "factory_aftersales": ("AFTERSALES", "MATERIAL", "QUALITY"),
    "merchant_owner": ("FLOW", "AFTERSALES", "QUALITY"),
    "merchant_senior_manager": ("FLOW", "AFTERSALES", "QUALITY"),
    "merchant_manager": ("FLOW", "AFTERSALES", "QUALITY"),
    "merchant_sales": ("FLOW", "AFTERSALES", "QUALITY"),
    "merchant_stock": ("FLOW", "QUALITY"),
    "merchant_assistant": ("FLOW",),
    "merchant_electrician": ("FLOW", "AFTERSALES"),
    "supplier_owner": ("MATERIAL", "AFTERSALES", "QUALITY"),
    "supplier_manager": ("MATERIAL", "AFTERSALES", "QUALITY"),
    "supplier_sales": ("MATERIAL", "AFTERSALES"),
    "supplier_technical": ("MATERIAL", "AFTERSALES", "QUALITY"),
    "service_owner": ("AFTERSALES",),
    "service_electrician": ("AFTERSALES",),
}

DASHBOARD_NAMES = {
    "factory_admin": "厂家经营全链路驾驶舱",
    "factory_manager": "厂家经营全链路驾驶舱",
    "factory_owner": "厂家经营全链路驾驶舱",
    "factory_senior_manager": "厂家经营全链路驾驶舱",
    "factory_chief_engineer": "质量与技术风险驾驶舱",
    "factory_sales": "渠道与产品流转驾驶舱",
    "factory_matching": "物料与供应商驾驶舱",
    "factory_aftersales": "售后服务驾驶舱",
    "merchant_owner": "商家经营与库存驾驶舱",
    "supplier_owner": "供应商物料质量驾驶舱",
    "supplier_manager": "供应商物料质量驾驶舱",
}


def modules_for_role(role_code):
    return ROLE_MODULES.get(role_code, ("FLOW",))
