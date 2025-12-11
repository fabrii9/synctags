from odoo import models, fields

class ProcessedOrderLog(models.Model):
    _name = 'processed.order.log'
    _description = 'Registro de Órdenes Procesadas'

    order_name = fields.Char(string="Nombre de Orden", required=True)
    label_name = fields.Char(string="Nombre de Etiqueta")
    processed_date = fields.Datetime(string="Fecha Procesada", required=True)
    synctags_id = fields.Many2one('synctags.synctags', string="SyncTags", ondelete='cascade')
