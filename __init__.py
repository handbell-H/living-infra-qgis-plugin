def classFactory(iface):
    from .living_infra import LivingInfraPlugin
    return LivingInfraPlugin(iface)
