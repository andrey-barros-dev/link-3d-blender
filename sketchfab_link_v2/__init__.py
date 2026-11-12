"""Sketchfab Link V2 — implementação independente, sem código do add-on original."""
bl_info = {
    'name': 'Sketchfab Link V2',
    'author': 'Andrey / projeto independente',
    'version': (2, 5, 3),
    'blender': (5, 2, 0),
    'location': 'View3D > Sidebar > Link 3D V2',
    'description': 'Recebe capturas do viewer, organiza modelos e reconstrói animações',
    'category': 'Import-Export',
}


def register():
    from . import ui
    ui.register()


def unregister():
    from . import service, ui
    service.stop()
    ui.unregister()
