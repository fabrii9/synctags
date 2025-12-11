python
from odoo import models, fields, api
from odoo.exceptions import UserError

class SynctagsProcessWizard(models.TransientModel):
_name = 'synctags.process.wizard'
_description = 'Procesamiento de etiquetas de Synctags'
synctags_id = fields.Many2one('synctags.synctags', string='Configuración', required=True)
resultado = fields.Text(string='Resultado', readonly=True)

def action_procesar(self):
    self.synctags_id.process_orders()
    self.resultado = self.synctags_id.result_summary
    return {
        'type': 'ir.actions.act_window',
        'res_model': 'synctags.process.wizard',
        'res_id': self.id,
        'view_mode': 'form',
        'target': 'new',
    }

