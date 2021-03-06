from pyramid.exceptions import URLDecodeError
from pyramid.compat import decode_path_info
from cornice.resource import resource
from jsonpointer import resolve_pointer
from schematics.exceptions import ModelValidationError
from pkg_resources import get_distribution
from couchdb.http import ResourceConflict
from logging import getLogger
from functools import partial
from time import sleep


from openregistry.api.utils import (
    error_handler,
    update_logging_context,
    set_modetest_titles,
    get_revision_changes,
    context_unpack,
    get_now,
    apply_data_patch
)

from openregistry.lots.core.constants import DEFAULT_LOT_TYPE

from openregistry.lots.core.traversal import factory

PKG = get_distribution(__package__)
LOGGER = getLogger(PKG.project_name)


oplotsresource = partial(resource,
                           error_handler=error_handler,
                           factory=factory)


def generate_lot_id(ctime, db, server_id=''):
    key = ctime.date().isoformat()
    lotIDdoc = 'lotID_' + server_id if server_id else 'lotID'
    while True:
        try:
            lotID = db.get(lotIDdoc, {'_id': lotIDdoc})
            index = lotID.get(key, 1)
            lotID[key] = index + 1
            db.save(lotID)
        except ResourceConflict:  # pragma: no cover
            pass
        except Exception:  # pragma: no cover
            sleep(1)
        else:
            break
    return 'UA-{:04}-{:02}-{:02}-{:06}{}'.format(ctime.year,
                                                 ctime.month,
                                                 ctime.day,
                                                 index,
                                                 server_id and '-' + server_id)


def extract_lot(request):
    try:
        # empty if mounted under a path in mod_wsgi, for example
        path = decode_path_info(request.environ['PATH_INFO'] or '/')
    except KeyError:
        path = '/'
    except UnicodeDecodeError as e:
        raise URLDecodeError(e.encoding, e.object, e.start, e.end, e.reason)

    lot_id = ""
    # extract lot id
    parts = path.split('/')
    if len(parts) < 4 or parts[3] != 'lots':
        return

    lot_id = parts[4]
    return extract_lot_adapter(request, lot_id)


def extract_lot_adapter(request, lot_id):
    db = request.registry.db
    doc = db.get(lot_id)
    if doc is None or doc.get('doc_type') != 'Lot':
        request.errors.add('url', 'lot_id', 'Not Found')
        request.errors.status = 404
        raise error_handler(request)

    return request.lot_from_data(doc)


def lot_from_data(request, data, raise_error=True, create=True):
    lotType = data.get('lotType', DEFAULT_LOT_TYPE)
    model = request.registry.lotTypes.get(lotType)
    if model is None and raise_error:
        request.errors.add('body', 'lotType', 'Not implemented')
        request.errors.status = 415
        raise error_handler(request)
    update_logging_context(request, {'lot_type': lotType})
    if model is not None and create:
        model = model(data)
    return model


def apply_patch(request, data=None, save=True, src=None):
    data = request.validated['data'] if data is None else data
    patch = data and apply_data_patch(src or request.context.serialize(), data)
    if patch:
        request.context.import_data(patch)
        if save:
            return save_lot(request)


class isLot(object):
    """ Route predicate. """

    def __init__(self, val, config):
        self.val = val

    def text(self):
        return 'lotType = %s' % (self.val,)

    phash = text

    def __call__(self, context, request):
        if request.lot is not None:
            return getattr(request.lot, 'lotType', None) == self.val
        return False


def register_lotType(config, model):
    """Register a lotType.
    :param config:
        The pyramid configuration object that will be populated.
    :param model:
        The lot model class
    """
    config.registry.lotTypes[model.lotType.default] = model


class SubscribersPicker(isLot):
    """ Subscriber predicate. """

    def __call__(self, event):
        if event.lot is not None:
            return getattr(event.lot, 'lotType', None) == self.val
        return False


def lot_serialize(request, lot_data, fields):
    import pdb; pdb.set_trace()
    lot = request.lot_from_data(lot_data, raise_error=False)
    if lot is None:
        return dict([(i, lot_data.get(i, '')) for i in ['lotType', 'dateModified', 'id']])
    return dict([(i, j) for i, j in lot.serialize(lot.status).items() if i in fields])


def save_lot(request):
    lot = request.validated['lot']
    if lot.mode == u'test':
        set_modetest_titles(lot)
    patch = get_revision_changes(lot.serialize("plain"), request.validated['lot_src'])
    if patch:
        now = get_now()
        status_changes = [
            p
            for p in patch
            if not p['path'].startswith('/bids/') and p['path'].endswith("/status") and p['op'] == "replace"
        ]
        for change in status_changes:
            obj = resolve_pointer(lot, change['path'].replace('/status', ''))
            if obj and hasattr(obj, "date"):
                date_path = change['path'].replace('/status', '/date')
                if obj.date and not any([p for p in patch if date_path == p['path']]):
                    patch.append({"op": "replace",
                                  "path": date_path,
                                  "value": obj.date.isoformat()})
                elif not obj.date:
                    patch.append({"op": "remove", "path": date_path})
                obj.date = now
        lot.revisions.append(type(lot).revisions.model_class({
            'author': request.authenticated_userid,
            'changes': patch,
            'rev': lot.rev
        }))
        old_dateModified = lot.dateModified
        if getattr(lot, 'modified', True):
            lot.dateModified = now
        try:
            lot.store(request.registry.db)
        except ModelValidationError, e:
            for i in e.message:
                request.errors.add('body', i, e.message[i])
            request.errors.status = 422
        except ResourceConflict, e:  # pragma: no cover
            request.errors.add('body', 'data', str(e))
            request.errors.status = 409
        except Exception, e:  # pragma: no cover
            request.errors.add('body', 'data', str(e))
        else:
            LOGGER.info('Saved lot {}: dateModified {} -> {}'.format(lot.id, old_dateModified and old_dateModified.isoformat(), lot.dateModified.isoformat()),
                        extra=context_unpack(request, {'MESSAGE_ID': 'save_lot'}, {'RESULT': lot.rev}))
            return True
