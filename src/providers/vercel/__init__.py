from providers.registry import register_vm_provider
from providers.vercel.provider import VercelProvider

register_vm_provider(VercelProvider)
