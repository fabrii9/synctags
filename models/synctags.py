# -*- coding: utf-8 -*-

import socket
import os
import base64
import logging
import xmlrpc.client
from odoo import models, fields
from odoo.exceptions import UserError
import time
from concurrent.futures import ThreadPoolExecutor
import re

_logger = logging.getLogger(__name__)


class SyncTags(models.Model):
    _name = 'synctags.synctags'
    _description = 'SyncTags'

    # --- Conexión remota ---
    name = fields.Char(string="Nombre", required=True)
    url = fields.Char(string="URL", required=True)
    db = fields.Char(string="Base de Datos", required=True)
    username = fields.Char(string="Usuario", required=True)
    password = fields.Char(string="Contraseña", required=True)  # opcional: usar ir.config_parameter
    directory = fields.Char(string="Directorio etiquetas", required=True)

    # --- Filtros para la búsqueda remota (sin M2O locales) ---
    type_remote_id = fields.Integer(string="Tipo de pedido (ID remoto)", required=True)
    tag = fields.Char(string="Etiqueta (ID M2M remoto)", required=True)
    delivery_status = fields.Char(string="Estado de la entrega", required=True)
    team_remote_id = fields.Integer(
        string="Equipo de venta (ID remoto)"
    )  # opcional, no usado si no lo agregás al dominio
    order_line_filter = fields.Char(
        string="Filtro en líneas de pedido",
        default="Mercado Envios Coleta",
        required=True
    )

    # --- Performance/impresión ---
    max_threads = fields.Integer(
        string="Número de Hilos",
        default=5,
        required=True,
        help="Número máximo de hilos para descargas en paralelo."
    )
    batch_size = fields.Integer(
        string="Tamaño de Lote",
        default=20,
        required=True,
        help="Cantidad de adjuntos a procesar por lote."
    )

    # Impresora
    printer_ip = fields.Char(string="IP de la impresora", required=True)
    printer_port = fields.Integer(string="Puerto de la impresora", default=9100, required=True)

    # CUPS
    cups_enabled = fields.Boolean(string="Usar CUPS como respaldo", default=False)
    cups_printer = fields.Char(string="Nombre de la impresora CUPS")

    # Orden de impresión
    print_order = fields.Selection(
        [('asc', 'Menor a Mayor'), ('desc', 'Mayor a Menor')],
        string="Orden de Impresión",
        default='desc',
        required=True
    )

    result_summary = fields.Text(string="Resumen de Resultados", readonly=True)

    # Pausa
    pause_after = fields.Integer(string="Pausar después de (número de etiquetas)", default=10, required=True)
    pause_duration = fields.Integer(string="Duración de la pausa (segundos)", default=5, required=True)

    # Tamaño y resolución de la etiqueta
    printer_dpi = fields.Selection(
        [('203', '203 dpi'), ('300', '300 dpi')],
        string="Resolución (dpi)",
        default='203',
        required=True
    )
    label_width_mm = fields.Integer(string="Ancho etiqueta (mm)", default=100, required=True)
    label_height_mm = fields.Integer(string="Alto etiqueta (mm)", default=50, required=True)

    # ---------------------------
    # Boton test impresora
    # ---------------------------
    def test_printer(self):
        """Envía una etiqueta de prueba a la impresora actual."""
        for record in self:
            try:
                # ZPL simple de prueba
                sample_zpl = b"^XA\n^FO50,50^ADN,36,20^FDTest Impresora^FS\n^XZ"

                # Ajustar al tamaño configurado (usa el mismo método que print_labels)
                data_to_send = record._apply_label_size(sample_zpl)

                sent = False
                errors = []

                # Intento RAW directo a la impresora
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(5)
                    sock.connect((record.printer_ip, record.printer_port))
                    sock.sendall(data_to_send)
                    sock.close()
                    sent = True
                except Exception as e:
                    errors.append(f"RAW: {e}")
                    _logger.error(f"Error RAW en test_printer: {e}")

                # Fallback CUPS si está habilitado
                if not sent and record.cups_enabled and record.cups_printer:
                    try:
                        import subprocess
                        process = subprocess.Popen(
                            ['lp', '-d', record.cups_printer],
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                        )
                        stdout, stderr = process.communicate(input=data_to_send)
                        if process.returncode != 0:
                            raise UserError(
                                stderr.decode('utf-8') or 'Error desconocido en CUPS (test_printer).'
                            )
                        sent = True
                    except Exception as e:
                        errors.append(f"CUPS: {e}")
                        _logger.error(f"Error CUPS en test_printer: {e}")

                if not sent:
                    msg = f"No se pudo imprimir la etiqueta de prueba: {'; '.join(errors)}"
                    record.send_notification("Error en prueba de impresora", msg, 'danger', True)
                    raise UserError(msg)

                # OK
                record.send_notification(
                    "Prueba de impresora",
                    f"Se envió una etiqueta de prueba a {record.printer_ip}:{record.printer_port}.",
                )
                _logger.info(
                    f"Etiqueta de prueba enviada a {record.printer_ip}:{record.printer_port}"
                )

            except Exception as e:
                # Si algo se rompe, lo mostramos bien en la UI
                _logger.error(f"Error en test_printer: {e}")
                raise UserError(f"Error en prueba de impresora: {e}")

    # ---------------------------
    # Conexión XML-RPC centralizada
    # ---------------------------
    def connection(self):
        for record in self:
            try:
                url, db, username, password = record.url, record.db, record.username, record.password
                common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
                uid = common.authenticate(db, username, password, {})
                if not uid:
                    raise UserError("Autenticación fallida. Verifique las credenciales.")
                models_proxy = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
                return models_proxy, uid, db, password
            except Exception as e:
                raise UserError(f"Error conectando al servidor: {e}")

    # ---------------------------
    # Wrapper XML-RPC con retry (anti 429)
    # ---------------------------
    def _execute_kw_with_retry(self, models_proxy, db, uid, password,
                               model_name, method, args, kwargs=None):
        """Wrapper centralizado para execute_kw con manejo de HTTP 429 (Too Many Requests)
        y reintentos con backoff incremental."""
        kwargs = kwargs or {}
        max_retries = 5
        base_backoff = 2  # segundos

        attempt = 0
        while True:
            try:
                return models_proxy.execute_kw(
                    db,
                    uid,
                    password,
                    model_name,
                    method,
                    args,
                    kwargs,
                )
            except xmlrpc.client.ProtocolError as e:
                # Rate limit del servidor remoto → reintentamos
                if getattr(e, "errcode", None) == 429 and attempt < max_retries:
                    attempt += 1
                    delay = base_backoff * attempt
                    _logger.warning(
                        "HTTP 429 en synctags (modelo %s, método %s). "
                        "Reintento %s/%s en %ss",
                        model_name,
                        method,
                        attempt,
                        max_retries,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                # Si agotamos reintentos o es otro código, relanzamos
                raise
            except Exception:
                # Otros errores, los dejamos subir tal cual
                raise

    # ---------------------------
    # Notificaciones UI
    # ---------------------------
    def send_notification(self, title, message, message_type='info', sticky=False):
        payload = {'title': title, 'message': message, 'sticky': sticky, 'type': message_type}
        self.env['bus.bus'].sudo()._sendone(self.env.user.partner_id, 'simple_notification', payload)

    # ---------------------------
    # Helpers XML-RPC (bulk)
    # ---------------------------
    def _get_pickings_bulk(self, models_proxy, uid, db, password, sale_order_ids):
        try:
            return self._execute_kw_with_retry(
                models_proxy,
                db,
                uid,
                password,
                'stock.picking',
                'search_read',
                [[['sale_id', 'in', sale_order_ids]]],
                {'fields': ['id', 'name', 'sale_id'], 'limit': False},
            )
        except Exception as e:
            _logger.error(f"Error obteniendo las entregas: {e}")
            raise UserError(f"Error obteniendo las entregas: {e}")

    def _get_attachments_bulk(self, models_proxy, uid, db, password, picking_ids):
        try:
            return self._execute_kw_with_retry(
                models_proxy,
                db,
                uid,
                password,
                'ir.attachment',
                'search_read',
                [[
                    ['res_model', '=', 'stock.picking'],
                    ['res_id', 'in', picking_ids],
                ]],
                {
                    'fields': ['name', 'datas', 'res_id'],
                    'limit': False,
                    'context': {'bin_size': False},
                },
            )
        except Exception as e:
            _logger.error(f"Error obteniendo los adjuntos: {e}")
            raise UserError(f"Error obteniendo los adjuntos: {e}")

    # ---------------------------
    # Obtención de órdenes
    # ---------------------------
    def get_orders(self):
        """Órdenes remotas que cumplan criterios y tengan al menos un adjunto .txt en sus entregas."""
        models_proxy, uid, db, password = self.connection()
        for record in self:
            try:
                criteria = [
                    ('delivery_status', '=', record.delivery_status),
                    ('type_id', '=', record.type_remote_id),
                    ('order_line.name', 'ilike', record.order_line_filter),
                    ('tag_ids', 'not ilike', 'impreso'),
                ]
                # if record.team_remote_id:
                #     criteria.append(('team_id', '=', record.team_remote_id))

                orders = self._execute_kw_with_retry(
                    models_proxy,
                    db,
                    uid,
                    password,
                    'sale.order',
                    'search_read',
                    [criteria],
                    {'fields': ['id', 'name'], 'limit': False},
                )
                if not orders:
                    return []

                order_ids = [o['id'] for o in orders]

                # Entregas asociadas a esas órdenes
                pickings = self._get_pickings_bulk(
                    models_proxy,
                    uid,
                    db,
                    password,
                    order_ids,
                )
                if not pickings:
                    return []

                picking_ids = [p['id'] for p in pickings]

                # Adjuntos de esas entregas
                attachments = self._get_attachments_bulk(
                    models_proxy,
                    uid,
                    db,
                    password,
                    picking_ids,
                )
                if not attachments:
                    return []

                # Solo adjuntos .txt
                attachments_txt = [a for a in attachments if a['name'].endswith('.txt')]
                if not attachments_txt:
                    return []

                picking_ids_with_txt = {a['res_id'] for a in attachments_txt}
                picking_to_order = {
                    p['id']: p['sale_id'][0]
                    for p in pickings
                    if p['id'] in picking_ids_with_txt and p.get('sale_id')
                }
                filtered_order_ids = set(picking_to_order.values())
                return [o for o in orders if o['id'] in filtered_order_ids]

            except Exception as e:
                _logger.error(f"Error en get_orders: {e}")
                raise UserError(f"Error obteniendo las órdenes: {e}")

    # ---------------------------
    # Descarga de adjuntos (sin ORM dentro de threads)
    # ---------------------------
    def download_attachment(self, file_data, picking_id, file_name, order_name):
        """Descarga un adjunto al filesystem.

        IMPORTANTE: este método se ejecuta dentro de hilos (ThreadPoolExecutor),
        por lo tanto NO debe usar self.env, UserError ni nada del ORM.
        Solo loguea y lanza excepciones normales.
        """
        # Usamos el primer record (en este caso, siempre se llama sobre un solo registro)
        record = self[0]
        try:
            _logger.info(
                f"Inicio de descarga: Orden={order_name}, Picking={picking_id}, Archivo={file_name}"
            )

            # Crear directorio si no existe
            if not os.path.exists(record.directory):
                os.makedirs(record.directory, exist_ok=True)

            safe_file_name = f"{order_name}_{picking_id}_{file_name.replace('/', '_')}"
            file_path = os.path.join(record.directory, safe_file_name)

            # Decodificar contenido
            file_content = base64.b64decode(file_data or b'')
            if not file_content:
                # Lanzamos una excepción normal, que se verá luego en el hilo principal
                raise Exception(f"El archivo {file_name} está vacío o corrupto.")

            # Guardar archivo
            with open(file_path, 'wb') as f:
                f.write(file_content)

            _logger.info(f"Archivo descargado exitosamente: {file_path}")

        except Exception as e:
            # Solo logueamos y re-lanzamos una Exception normal
            _logger.error(f"Error al guardar el archivo {file_name}: {e}")
            # Esto hará que future.result() en process_orders falle
            raise Exception(f"Error al guardar el archivo {file_name}: {e}")


    def clear_labels(self):
        """Elimina los .txt del directorio configurado."""
        for record in self:
            try:
                if not record.directory or not os.path.isdir(record.directory):
                    continue
                for file in os.listdir(record.directory):
                    if file.endswith('.txt'):
                        os.remove(os.path.join(record.directory, file))
                        _logger.info(f"Archivo eliminado: {os.path.join(record.directory, file)}")
            except Exception as e:
                _logger.error(f"Error al borrar las etiquetas: {e}")
                raise UserError(f"Error al borrar las etiquetas: {e}")

    # ---------------------------
    # Proceso general
    # ---------------------------
    def process_orders(self):
        for record in self:
            try:
                record.send_notification("Procesamiento Iniciado", "Se ha iniciado el procesamiento de órdenes.")
                _logger.info("Iniciando el procesamiento de órdenes...")

                record.clear_labels()

                orders = record.get_orders()
                if not orders:
                    record.send_notification(
                        "Sin Órdenes",
                        "No se encontraron órdenes para procesar.",
                        'warning',
                        True,
                    )
                    record.result_summary = "No se encontraron órdenes para procesar."
                    continue

                record.send_notification(
                    "Órdenes Obtenidas",
                    f"Se obtuvieron {len(orders)} órdenes para procesar.",
                )
                _logger.info(f"Órdenes: {len(orders)}")

                models_proxy, uid, db, password = record.connection()
                sale_order_ids = [o['id'] for o in orders]

                # Primer pool: obtener pickings y adjuntos
                with ThreadPoolExecutor(max_workers=record.max_threads) as executor:
                    pickings_future = executor.submit(
                        record._get_pickings_bulk, models_proxy, uid, db, password, sale_order_ids
                    )
                    pickings = pickings_future.result()

                    if not pickings:
                        record.send_notification(
                            "Sin Entregas",
                            "No se encontraron entregas para estas órdenes.",
                            'warning',
                            True,
                        )
                        record.result_summary = "No se encontraron entregas para estas órdenes."
                        continue

                    picking_ids = [p['id'] for p in pickings]

                    attachments_future = executor.submit(
                        record._get_attachments_bulk, models_proxy, uid, db, password, picking_ids
                    )
                    attachments = attachments_future.result()

                if not attachments:
                    record.send_notification(
                        "Sin Adjuntos",
                        "No se encontraron adjuntos para las entregas.",
                        'warning',
                        True,
                    )
                    record.result_summary = "No se encontraron adjuntos para las entregas."
                    continue

                attachments_txt = [a for a in attachments if a['name'].endswith('.txt')]
                if not attachments_txt:
                    record.send_notification(
                        "Sin Etiquetas TXT",
                        "No se encontraron adjuntos .txt en las entregas.",
                        'warning',
                        True,
                    )
                    record.result_summary = "No se encontraron adjuntos .txt en las entregas."
                    continue

                picking_to_order = {}
                for p in pickings:
                    if p.get('sale_id'):
                        picking_to_order[p['id']] = p['sale_id'][0]

                total_attachments = len(attachments_txt)
                record.send_notification(
                    "Descarga de Etiquetas",
                    f"Se encontraron {total_attachments} etiquetas .txt para procesar.",
                )
                _logger.info(f"Total de etiquetas .txt a procesar: {total_attachments}")

                batches = [
                    attachments_txt[i:i + record.batch_size]
                    for i in range(0, total_attachments, record.batch_size)
                ]

                for batch_index, batch in enumerate(batches, start=1):
                    _logger.info(
                        f"Procesando lote {batch_index}/{len(batches)} con {len(batch)} adjuntos"
                    )
                    with ThreadPoolExecutor(max_workers=record.max_threads) as executor:
                        futures = []
                        for attachment in batch:
                            file_data = attachment['datas']
                            picking_id = attachment['res_id']
                            file_name = attachment['name']
                            order_id = picking_to_order.get(picking_id)

                            if not order_id:
                                _logger.warning(
                                    f"Picking {picking_id} sin orden asociada. Saltando."
                                )
                                continue

                            order_name = next(
                                (o['name'] for o in orders if o['id'] == order_id),
                                f"SO_{order_id}",
                            )

                            futures.append(
                                executor.submit(
                                    record.download_attachment,
                                    file_data,
                                    picking_id,
                                    file_name,
                                    order_name,
                                )
                            )

                        for future in futures:
                            future.result()

                record.result_summary = (
                    f"Descarga completada. Total de etiquetas procesadas: {total_attachments}."
                )
                record.send_notification(
                    "Procesamiento Completado",
                    f"Descarga completada. Total de etiquetas procesadas: {total_attachments}.",
                )

                record.print_labels()

            except Exception as e:
                _logger.error(f"Error en el procesamiento de órdenes: {e}")
                record.send_notification(
                    "Error en Procesamiento",
                    f"Error en el procesamiento de órdenes: {e}",
                    'danger',
                    True,
                )
                raise UserError(f"Error en el procesamiento de órdenes: {e}")

    # ---------------------------
    # Impresión
    # ---------------------------
    def check_printer_connection(self):
        for record in self:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3)
                sock.connect((record.printer_ip, record.printer_port))
                sock.close()
                record.send_notification(
                    "Conexión Exitosa",
                    f"Se pudo conectar a la impresora en {record.printer_ip}:{record.printer_port}.",
                )
            except Exception as e:
                record.send_notification(
                    "Error de Conexión",
                    f"No se pudo conectar a la impresora: {e}",
                    'danger',
                    True,
                )
                raise UserError(f"No se pudo conectar a la impresora: {e}")

    def print_labels(self):
        for record in self:
            try:
                if not record.directory or not os.path.isdir(record.directory):
                    raise UserError("El directorio de etiquetas no es válido.")

                files = [
                    os.path.join(record.directory, f)
                    for f in os.listdir(record.directory)
                    if f.lower().endswith('.txt')
                ]

                if not files:
                    record.send_notification(
                        "Sin Etiquetas",
                        "No hay archivos .txt para imprimir.",
                        'warning',
                        True,
                    )
                    record.result_summary = "No hay archivos .txt para imprimir."
                    return

                def extract_order_number(filename):
                    base = os.path.basename(filename)
                    match = re.search(r'(OV\s+\d{4}-\d{8})', base)
                    if not match:
                        match = re.search(r'(\d{4}-\d{8})', base)
                    if not match:
                        return 0
                    return int(
                        match.group(0)
                        .replace('OV', '')
                        .replace(' ', '')
                        .replace('-', '')
                    )

                files.sort(
                    key=extract_order_number,
                    reverse=(record.print_order == 'desc'),
                )

                models_proxy, uid, db, password = record.connection()
                pause_counter = 0
                order_cache = {}

                for file_path in files:
                    file_name = os.path.basename(file_path)
                    order_name = (
                        file_name.split('_')[0] if '_' in file_name else file_name
                    )

                    with open(file_path, 'rb') as f:
                        raw_data = f.read()

                    if raw_data.startswith(b'^') or raw_data.startswith(b'N'):
                        processed_data = self._apply_label_size(raw_data)
                    else:
                        processed_data = raw_data

                    sent = False
                    errors = []

                    # RAW
                    try:
                        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        sock.settimeout(5)
                        sock.connect((record.printer_ip, record.printer_port))
                        sock.sendall(processed_data)
                        sock.close()
                        sent = True
                    except Exception as e:
                        errors.append(f"RAW: {e}")
                        _logger.error(f"Error RAW al imprimir {file_name}: {e}")

                    # CUPS fallback
                    if not sent and record.cups_enabled and record.cups_printer:
                        try:
                            import subprocess
                            process = subprocess.Popen(
                                ['lp', '-d', record.cups_printer],
                                stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                            )
                            stdout, stderr = process.communicate(input=processed_data)
                            if process.returncode != 0:
                                raise UserError(
                                    stderr.decode('utf-8')
                                    or 'Error desconocido en CUPS.'
                                )
                            sent = True
                        except Exception as e:
                            errors.append(f"CUPS: {e}")
                            _logger.error(f"Error CUPS al imprimir {file_name}: {e}")

                    if not sent:
                        record.send_notification(
                            "Error al Imprimir",
                            f"No se pudo imprimir la etiqueta {file_name}: {'; '.join(errors)}",
                            'danger',
                            True,
                        )
                        continue

                    record.send_notification(
                        "Etiqueta Impresa",
                        f"Se imprimió la etiqueta {file_name}.",
                    )

                    self.mark_order_as_processed(
                        models_proxy, uid, db, password, order_name, file_name, order_cache
                    )

                    pause_counter += 1
                    if pause_counter >= record.pause_after:
                        record.send_notification(
                            "Pausa de Impresión",
                            f"Se imprimieron {pause_counter} etiquetas. "
                            f"Pausando {record.pause_duration} segundos.",
                        )
                        time.sleep(record.pause_duration)
                        pause_counter = 0

                record.send_notification(
                    "Impresión Completada",
                    "Todas las etiquetas fueron procesadas.",
                )

            except Exception as e:
                _logger.error(f"Error durante la impresión de etiquetas: {e}")
                raise UserError(f"Error durante la impresión de etiquetas: {e}")

    # ---------------------------
    # Marcado de órdenes remotas
    # ---------------------------
    def mark_order_as_processed(self, models_proxy, uid, db, password,
                                order_name, label_name, order_cache):
        for record in self:
            try:
                if order_name in order_cache:
                    order_id, tag_ids = order_cache[order_name]
                else:
                    order = self._execute_kw_with_retry(
                        models_proxy,
                        db,
                        uid,
                        password,
                        'sale.order',
                        'search_read',
                        [[['name', '=', order_name]]],
                        {'fields': ['id', 'tag_ids'], 'limit': 1},
                    )
                    if not order:
                        record.send_notification(
                            "Orden no Encontrada",
                            f"No se encontró la orden: {order_name}",
                            'warning',
                            True,
                        )
                        raise UserError(f"No se encontró la orden: {order_name}")

                    order_id = order[0]['id']
                    tag_ids = set(order[0]['tag_ids'] or [])
                    order_cache[order_name] = (order_id, tag_ids)

                tag_id_int = int(record.tag)
                if tag_id_int not in tag_ids:
                    self._execute_kw_with_retry(
                        models_proxy,
                        db,
                        uid,
                        password,
                        'sale.order',
                        'write',
                        [[order_id], {'tag_ids': [(4, tag_id_int)]}],
                    )
                    tag_ids.add(tag_id_int)
                    order_cache[order_name] = (order_id, tag_ids)

                self.env['processed.order.log'].create({
                    'order_name': order_name,
                    'label_name': label_name,
                    'processed_date': fields.Datetime.now(),
                    'synctags_id': record.id,
                })

                record.send_notification(
                    "Orden Procesada",
                    f"La orden {order_name} fue marcada como procesada.",
                )
                _logger.info(f"Orden {order_name} marcada como procesada.")

            except Exception as e:
                record.send_notification(
                    "Error al Marcar Orden",
                    f"Error con {order_name}: {str(e)}",
                    'danger',
                    True,
                )
                _logger.error(f"Error marcando orden como procesada: {e}")
                raise UserError(f"Error marcando orden como procesada: {e}")

    # ---------------------------
    # Ajuste de tamaño de etiqueta ZPL/EPL
    # ---------------------------
    def _dots_per_mm(self):
        dpi = int(self.printer_dpi or 203)
        return dpi / 25.4  # 1 pulgada = 25.4mm

    def _apply_label_size(self, raw_data):
        """Detecta ZPL/EPL y ajusta el tamaño de la etiqueta según la config."""
        try:
            txt = raw_data.decode('latin-1', errors='ignore')
        except Exception:
            return raw_data

        dots_per_mm = self._dots_per_mm()
        pw = int(self.label_width_mm * dots_per_mm)
        ll = int(self.label_height_mm * dots_per_mm)

        # ZPL
        if txt.startswith('^'):
            lines = txt.split('\n')
            found_pw = False
            found_ll = False
            new_lines = []
            for line in lines:
                if line.startswith('^PW'):
                    new_lines.append(f"^PW{pw}")
                    found_pw = True
                elif line.startswith('^LL'):
                    new_lines.append(f"^LL{ll}")
                    found_ll = True
                else:
                    new_lines.append(line)
            if not found_pw:
                new_lines.insert(1, f"^PW{pw}")
            if not found_ll:
                new_lines.insert(2, f"^LL{ll}")
            return ("\n".join(new_lines)).encode('latin-1', errors='ignore')

        # EPL
        if txt.startswith('N'):
            b = raw_data
            lines = b.split(b"\n")
            out = []
            inserted = False
            for line in lines:
                out.append(line)
                if not inserted and line.strip() == b"N":
                    out.append(f"q{pw}\n".encode("ascii"))
                    out.append(f"Q{ll},24\n".encode("ascii"))
                    inserted = True
            return b"".join(out)

        return raw_data
