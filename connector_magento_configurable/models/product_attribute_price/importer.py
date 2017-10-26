# -*- coding: utf-8 -*-
# Copyright 2017 Akretion
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html)

from odoo.addons.component.core import Component
from odoo.addons.connector.components.mapper import mapping
from odoo.addons.connector.exception import MappingError


class ProductAttributePriceBatchImporter(Component):
    """ Import the Magento Product Attributes.
    """
    _name = 'magento.product.attribute.price.batch.importer'
    _inherit = 'magento.direct.batch.importer'
    _apply_on = ['magento.product.attribute.price']

    def run(self, filters=None):
        """ Run the synchronization """
        price = filters['price']
        price['product_id'] = filters['product_id']
        price['magento_value'] = filters['magento_value']
        price['external_id'] = price['value_index']
        price['external_id'] += '_' + price['product_id']
        self._import_record(price)


class ProductAttributePriceImporter(Component):
    _name = 'magento.product.attribute.price.importer'
    _inherit = 'magento.importer'
    _apply_on = ['magento.product.attribute.price']

    def _get_magento_data(self, storeview_id=None):
        """
        In this case, the magento_record contains all the data to insert
        """
        record = self.magento_record
        binder = self.binder_for('magento.product.product')
        product_binding = binder.to_internal(record['product_id'])

        if not product_binding:
            raise MappingError("The product with "
                               "magento id %s is not imported." %
                               record['product_id'])

        record['template'] = product_binding.product_tmpl_id
        return record

    def run(self, magento_record, force=False):
        self.magento_record = magento_record
        super(ProductAttributePriceImporter, self).run(
            magento_record['external_id'],
            force,
        )


class ProductAttributePriceImportMapper(Component):
    _name = 'magento.product.attribute.price.import.mapper'
    _inherit = 'magento.import.mapper'
    _apply_on = 'magento.product.attribute.price'

    direct = [
        ('pricing_value', 'price_extra'),
        ('external_id', 'external_id'),
    ]

    @mapping
    def price_extra(self, record):
        price = record['pricing_value']
        if record['is_percent'] == '1':
            price = (float(price)/100) * record['template'].list_price
        return {'price_extra': price}

    @mapping
    def backend_id(self, record):
        return {'backend_id': self.backend_record.id}

    @mapping
    def product_tmpl_id(self, record):
        return {
            'product_tmpl_id': record['template'].id,
        }

    @mapping
    def value_id(self, record):
        if not record.get('magento_value'):
            return

        value = record.get('magento_value').odoo_id
        return {
            'value_id': value.id,
            'magento_value_id': record.get('magento_value').id,
        }
