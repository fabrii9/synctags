# -*- coding: utf-8 -*-
# from odoo import http


# class Synctags(http.Controller):
#     @http.route('/synctags/synctags', auth='public')
#     def index(self, **kw):
#         return "Hello, world"

#     @http.route('/synctags/synctags/objects', auth='public')
#     def list(self, **kw):
#         return http.request.render('synctags.listing', {
#             'root': '/synctags/synctags',
#             'objects': http.request.env['synctags.synctags'].search([]),
#         })

#     @http.route('/synctags/synctags/objects/<model("synctags.synctags"):obj>', auth='public')
#     def object(self, obj, **kw):
#         return http.request.render('synctags.object', {
#             'object': obj
#         })
