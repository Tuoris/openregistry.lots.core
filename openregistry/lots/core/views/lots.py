# -*- coding: utf-8 -*-
from openregistry.lots.core.events import LotInitializeEvent
from openregistry.lots.core.design import (
    FIELDS, VIEW_MAP, CHANGES_VIEW_MAP, FEED
)

from openregistry.api.utils import (
    get_now, generate_id, json_view, set_ownership,
    context_unpack, APIResourceListing
)

from openregistry.lots.core.utils import (
    save_lot, lot_serialize, oplotsresource, generate_lot_id
)
from openregistry.lots.core.validation import (
    validate_lot_data,
    validate_post_lot_role,
)


@oplotsresource(name='Lots',
                path='/lots',
                description="Open Contracting compatible data exchange format.")
class LotsResource(APIResourceListing):

    def __init__(self, request, context):
        super(LotsResource, self).__init__(request, context)
        # params for listing
        self.VIEW_MAP = VIEW_MAP
        self.CHANGES_VIEW_MAP = CHANGES_VIEW_MAP
        self.FEED = FEED
        self.FIELDS = FIELDS
        self.serialize_func = lot_serialize
        self.object_name_for_listing = 'Lots'
        self.log_message_id = 'lot_list_custom'

    @json_view(content_type="application/json", permission='create_lot',
               validators=(validate_lot_data, validate_post_lot_role))
    def post(self):
        """This API request is targeted to creating new Lot."""
        lot_id = generate_id()
        lot = self.request.validated['lot']
        lot.id = lot_id
        if not lot.get('lotID'):
            lot.lotID = generate_lot_id(get_now(), self.db, self.server_id)
        self.request.registry.notify(LotInitializeEvent(lot))
        if self.request.json_body['data'].get('status') == 'draft':
            lot.status = 'draft'
        elif self.request.json_body['data'].get('status') == 'waiting':
            lot.status = 'waiting'
        else:
            self.request.errors.add('body', 'status',
                               'You can create only in draft or waiting statuses')
            self.request.errors.status = 403
            return
        set_ownership(lot, self.request)  # rewrite as subscriber?
        self.request.validated['lot'] = lot
        self.request.validated['lot_src'] = {}
        if save_lot(self.request):
            self.LOGGER.info('Created lot {} ({})'.format(lot_id, lot.lotID),
                        extra=context_unpack(self.request, {'MESSAGE_ID': 'lot_create'}, {'lot_id': lot_id, 'lotID': lot.lotID}))
            self.request.response.status = 201
            self.request.response.headers['Location'] = self.request.route_url('{}:Lot'.format(lot.lotType), lot_id=lot_id)
            return {
                'data': lot.serialize(lot.status),
                'access': {
                    'token': lot.owner_token
                }
            }
