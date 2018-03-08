# -*- coding: utf-8 -*-
# Copyright 2017 Akretion
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html)

from odoo import models, fields, api

from odoo.addons.component.core import Component
from odoo.addons.connector.components.mapper import mapping


class MagentoBackend(models.Model):
    _inherit = 'magento.backend'

    import_configurables_from_date = fields.Datetime(
        string='Import configurables from date',
    )

    @api.multi
    def import_product_configurable(self):
        self._import_from_date('magento.product.template',
                               'import_configurables_from_date')
        return True

    @api.model
    def _scheduler_import_product_configurable(self, domain=None):
        self._magento_backend('import_product_configurable', domain=domain)


class ProductImportMapper(Component):
    _inherit = 'magento.product.product.import.mapper'

    @mapping
    def magento_is_configurable(self, record):
        return {'magento_is_configurable': record['type_id'] == 'configurable'}


# class MagentoProductProduct(models.Model):
#     _inherit = 'magento.product.product'

#     transformed_at = fields.Date(
#         'Transformed At (from simple to templated product)'
#     )

#     magento_is_configurable = fields.Boolean(
#         'True if the product is a configurable (the parent)'
#     )


class MagentoConfigurableModelBinder(Component):
    _name = 'magento.configurable.binder'
    _inherit = 'magento.binder'
    _apply_on = [
        'magento.product.template',
        'magento.product.attribute',
        'magento.product.attribute.value',
        'magento.product.attribute.line',
        'magento.product.attribute.price',
    ]
