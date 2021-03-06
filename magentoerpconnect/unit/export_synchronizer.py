# -*- coding: utf-8 -*-
##############################################################################
#
#    Author: Guewen Baconnier
#    Copyright 2013 Camptocamp SA
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

import logging

from contextlib import contextmanager
from datetime import datetime

import psycopg2

from openerp import SUPERUSER_ID
from openerp.tools.translate import _
from openerp.tools import DEFAULT_SERVER_DATETIME_FORMAT
from openerp.addons.connector.queue.job import job, related_action
from openerp.addons.connector.unit.synchronizer import ExportSynchronizer
from openerp.addons.connector.exception import (IDMissingInBackend,
                                                RetryableJobError)
from .import_synchronizer import import_record
from ..connector import get_environment
from ..related_action import unwrap_binding

_logger = logging.getLogger(__name__)


"""

Exporters for Magento.

In addition to its export job, an exporter has to:

* check in Magento if the record has been updated more recently than the
  last sync date and if yes, delay an import
* call the ``bind`` method of the binder to update the last sync date

"""


class MagentoBaseExporter(ExportSynchronizer):
    """ Base exporter for Magento """

    def __init__(self, environment):
        """
        :param environment: current environment (backend, session, ...)
        :type environment: :py:class:`connector.connector.Environment`
        """
        super(MagentoBaseExporter, self).__init__(environment)
        self.binding_id = None
        self.magento_id = None

    def _delay_import(self):
        """ Schedule an import of the record.

        Adapt in the sub-classes when the model is not imported
        using ``import_record``.
        """
        # force is True because the sync_date will be more recent
        # so the import would be skipped
        assert self.magento_id
        import_record.delay(self.session, self.model._name,
                            self.backend_record.id, self.magento_id,
                            force=True)

    def _should_import(self):
        """ Before the export, compare the update date
        in Magento and the last sync date in OpenERP,
        if the former is more recent, schedule an import
        to not miss changes done in Magento.
        """
        assert self.binding_record
        if not self.magento_id:
            return False
        sync = self.binding_record.sync_date
        if not sync:
            return True
        record = self.backend_adapter.read(self.magento_id,
                                           attributes=['updated_at'])
        if not record['updated_at']:
            # in rare case it can be empty, in doubt, import it
            return False
        fmt = DEFAULT_SERVER_DATETIME_FORMAT
        sync_date = datetime.strptime(sync, fmt)
        magento_date = datetime.strptime(record['updated_at'], fmt)
        return sync_date < magento_date

    def _get_openerp_data(self):
        """ Return the raw OpenERP data for ``self.binding_id`` """
        return self.session.browse(self.model._name, self.binding_id)

    def _after_export(self):
        """ Run the after export"""
        return

    def run(self, binding_id, *args, **kwargs):
        """ Run the synchronization

        :param binding_id: identifier of the binding record to export
        """
        self.binding_id = binding_id
        self.binding_record = self._get_openerp_data()

        self.magento_id = self.binder.to_backend(self.binding_id)
        try:
            should_import = self._should_import()
        except IDMissingInBackend:
            self.magento_id = None
            should_import = False
        if should_import:
            self._delay_import()

        result = self._run(*args, **kwargs)

        self.binder.bind(self.magento_id, self.binding_id)
        # Commit so we keep the external ID when there are several
        # exports (due to dependencies) and one of them fails.
        # The commit will also release the lock acquired on the binding
        # record
        self.session.commit()
        self._after_export()
        return result

    def _run(self):
        """ Flow of the synchronization, implemented in inherited classes"""
        raise NotImplementedError


class MagentoExporter(MagentoBaseExporter):
    """ A common flow for the exports to Magento """

    def __init__(self, environment):
        """
        :param environment: current environment (backend, session, ...)
        :type environment: :py:class:`connector.connector.Environment`
        """
        super(MagentoExporter, self).__init__(environment)
        self.binding_record = None

    def _lock(self):
        """ Lock the binding record.

        Lock the binding record so we are sure that only one export
        job is running for this record if concurrent jobs have to export the
        same record.

        When concurrent jobs try to export the same record, the first one
        will lock and proceed, the others will fail to lock and will be
        retried later.

        This behavior works also when the export becomes multilevel
        with :meth:`_export_dependencies`. Each level will set its own lock
        on the binding record it has to export.

        """
        sql = ("SELECT id FROM %s WHERE ID = %%s FOR UPDATE NOWAIT" %
               self.model._table)
        try:
            self.session.cr.execute(sql, (self.binding_id, ),
                                    log_exceptions=False)
        except psycopg2.OperationalError:
            _logger.info('A concurrent job is already exporting the same '
                         'record (%s with id %s). Job delayed later.',
                         self.model._name, self.binding_id)
            raise RetryableJobError(
                'A concurrent job is already exporting the same record '
                '(%s with id %s). The job will be retried later.' %
                (self.model._name, self.binding_id))

    def _has_to_skip(self):
        """ Return True if the export can be skipped """
        return False

    @contextmanager
    def _retry_unique_violation(self):
        """ Context manager: catch Unique constraint error and retry the
        job later.

        When we execute several jobs workers concurrently, it happens
        that 2 jobs are creating the same record at the same time (binding
        record created by :meth:`_export_dependency`), resulting in:

            IntegrityError: duplicate key value violates unique
            constraint "magento_product_product_openerp_uniq"
            DETAIL:  Key (backend_id, openerp_id)=(1, 4851) already exists.

        In that case, we'll retry the import just later.

        .. warning:: The unique constraint must be created on the
                     binding record to prevent 2 bindings to be created
                     for the same Magento record.

        """
        try:
            yield
        except psycopg2.IntegrityError as err:
            if err.pgcode == psycopg2.errorcodes.UNIQUE_VIOLATION:
                raise RetryableJobError(
                    'A database error caused the failure of the job:\n'
                    '%s\n\n'
                    'Likely due to 2 concurrent jobs wanting to create '
                    'the same record. The job will be retried later.' % err)
            else:
                raise

    def _export_dependency(self, relation, binding_model, exporter_class=None,
                           binding_field='magento_bind_ids',
                           binding_extra_vals=None):
        """
        Export a dependency. The exporter class is a subclass of
        ``MagentoExporter``. If a more precise class need to be defined,
        it can be passed to the ``exporter_class`` keyword argument.

        .. warning:: a commit is done at the end of the export of each
                     dependency. The reason for that is that we pushed a record
                     on the backend and we absolutely have to keep its ID.

                     So you *must* take care not to modify the OpenERP
                     database during an export, excepted when writing
                     back the external ID or eventually to store
                     external data that we have to keep on this side.

                     You should call this method only at the beginning
                     of the exporter synchronization,
                     in :meth:`~._export_dependencies`.

        :param relation: record to export if not already exported
        :type relation: :py:class:`openerp.osv.orm.browse_record`
        :param binding_model: name of the binding model for the relation
        :type binding_model: str | unicode
        :param exporter_cls: :py:class:`openerp.addons.connector\
                                        .connector.ConnectorUnit`
                             class or parent class to use for the export.
                             By default: MagentoExporter
        :type exporter_cls: :py:class:`openerp.addons.connector\
                                       .connector.MetaConnectorUnit`
        :param binding_field: name of the one2many field on a normal
                              record that points to the binding record
                              (default: magento_bind_ids).
                              It is used only when the relation is not
                              a binding but is a normal record.
        :type binding_field: str | unicode
        :binding_extra_vals:  In case we want to create a new binding
                              pass extra values for this binding
        :type binding_extra_vals: dict
        """
        if not relation:
            return
        if exporter_class is None:
            exporter_class = MagentoExporter
        rel_binder = self.get_binder_for_model(binding_model)
        # wrap is typically True if the relation is for instance a
        # 'product.product' record but the binding model is
        # 'magento.product.product'
        wrap = relation._model._name != binding_model

        if wrap and hasattr(relation, binding_field):
            domain = [('openerp_id', '=', relation.id),
                      ('backend_id', '=', self.backend_record.id)]
            binding_ids = self.session.search(binding_model, domain)
            if binding_ids:
                assert len(binding_ids) == 1, (
                    'only 1 binding for a backend is '
                    'supported in _export_dependency')
                binding_id = binding_ids[0]
            # we are working with a unwrapped record (e.g.
            # product.category) and the binding does not exist yet.
            # Example: I created a product.product and its binding
            # magento.product.product and we are exporting it, but we need to
            # create the binding for the product.category on which it
            # depends.
            else:
                ctx = {'connector_no_export': True}
                with self.session.change_context(ctx):
                    with self.session.change_user(SUPERUSER_ID):
                        bind_values = {'backend_id': self.backend_record.id,
                                       'openerp_id': relation.id}
                        if binding_extra_vals:
                            bind_values.update(binding_extra_vals)
                        # If 2 jobs create it at the same time, retry
                        # one later. A unique constraint (backend_id,
                        # openerp_id) should exist on the binding model
                        with self._retry_unique_violation():
                            binding_id = self.session.create(binding_model,
                                                             bind_values)
                            # Eager commit to avoid having 2 jobs
                            # exporting at the same time. The constraint
                            # will pop if an other job already created
                            # the same binding. It will be caught and
                            # raise a RetryableJobError.
                            self.session.commit()
        else:
            # If magento_bind_ids does not exist we are typically in a
            # "direct" binding (the binding record is the same record).
            # If wrap is True, relation is already a binding record.
            binding_id = relation.id

        if not rel_binder.to_backend(binding_id):
            exporter = self.get_connector_unit_for_model(exporter_class,
                                                         binding_model)
            exporter.run(binding_id)

    def _export_dependencies(self):
        """ Export the dependencies for the record"""
        return

    def _map_data(self):
        """ Returns an instance of
        :py:class:`~openerp.addons.connector.unit.mapper.MapRecord`

        """
        return self.mapper.map_record(self.binding_record)

    def _validate_data(self, data):
        """ Check if the values to import are correct

        Pro-actively check before the ``Model.create`` or
        ``Model.update`` if some fields are missing

        Raise `InvalidDataError`
        """
        return

    def _create_data(self, map_record, fields=None, **kwargs):
        """ Get the data to pass to :py:meth:`_create` """
        return map_record.values(for_create=True, fields=fields, **kwargs)

    def _create(self, data):
        """ Create the Magento record """
        # special check on data before export
        self._validate_data(data)
        return self.backend_adapter.create(data)

    def _update_data(self, map_record, fields=None, **kwargs):
        """ Get the data to pass to :py:meth:`_update` """
        return map_record.values(fields=fields, **kwargs)

    def _update(self, data):
        """ Update an Magento record """
        assert self.magento_id
        # special check on data before export
        self._validate_data(data)
        self.backend_adapter.write(self.magento_id, data)

    def _run(self, fields=None):
        """ Flow of the synchronization, implemented in inherited classes"""
        assert self.binding_id
        assert self.binding_record

        if not self.magento_id:
            fields = None  # should be created with all the fields

        if self._has_to_skip():
            return

        # export the missing linked resources
        self._export_dependencies()

        # prevent other jobs to export the same record
        # will be released on commit (or rollback)
        self._lock()

        map_record = self._map_data()

        if self.magento_id:
            record = self._update_data(map_record, fields=fields)
            if not record:
                return _('Nothing to export.')
            self._update(record)
        else:
            record = self._create_data(map_record, fields=fields)
            if not record:
                return _('Nothing to export.')
            self.magento_id = self._create(record)
        return _('Record exported with ID %s on Magento.') % self.magento_id

class MagentoTranslationExporter(MagentoExporter):
    """ A common flow for the exports record with translation to Magento """

    def _get_translatable_field(self, fields):
        # find the translatable fields of the model
        # Note we consider that a translatable field in Magento
        # must be a translatable field in OpenERP and vice-versa
        # you can change this behaviour in your own module
        all_fields = self.model.fields_get(self.session.cr, self.session.uid,
                                   context=self.session.context)

        translatable_fields = [field for field, attrs in all_fields.iteritems()
                           if attrs.get('translate') and (not fields or field in fields)]
        return translatable_fields


    def _run(self, fields=None):
        default_lang = self.backend_record.default_lang_id
        session = self.session
        if session.context is None:
            session.context = {}
        session.context['lang'] = default_lang.code
        res = super(MagentoTranslationExporter, self)._run(fields)
        
        storeview_ids = session.search(
                'magento.storeview',
                [('backend_id', '=', self.backend_record.id)])
        storeviews = session.browse('magento.storeview', storeview_ids)
        lang_storeviews = [sv for sv in storeviews
                           if sv.lang_id and sv.lang_id != default_lang]
        if lang_storeviews:
            translatable_fields = self._get_translatable_field(fields)   
            if translatable_fields:
                for storeview in lang_storeviews:
                    session.context['lang'] = storeview.lang_id.code
                    self.binding_record = self._get_openerp_data()
                    map_record = self._map_data()
                    record = self._update_data(
                         map_record, fields=translatable_fields)
                    if not map_record:
                        return _('nothing to export.')
                    # special check on data before export
                    self._validate_data(record)
                    binder = self.get_binder_for_model('magento.storeview')
                    magento_storeview_id = binder.to_backend(storeview.id)
                    self.backend_adapter.write(
                        self.magento_id, record, magento_storeview_id)
        return res


@job
@related_action(action=unwrap_binding)
def export_record(session, model_name, binding_id, fields=None):
    """ Export a record on Magento """
    record = session.browse(model_name, binding_id)
    env = get_environment(session, model_name, record.backend_id.id)
    exporter = env.get_connector_unit(MagentoExporter)
    return exporter.run(binding_id, fields=fields)
