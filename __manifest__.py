# -*- coding: utf-8 -*-
{
    'name': "synctags",

    'summary': """
        Descarga etiquetas .txt de las entregas del Odoo remoto, las imprime
        y marca las órdenes como procesadas""",

    'author': "Printemps",
    'license': 'LGPL-3',

    'category': 'Uncategorized',
    'version': '20.0.1.0.0',

    "depends": ["base", "bus"],

    'data': [
        'security/ir.access.csv',
        'views/views.xml',
    ],
}
