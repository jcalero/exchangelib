from __future__ import unicode_literals

import base64
import logging
import mimetypes

from six import string_types

from .fields import BooleanField, TextField, IntegerField, URIField, DateTimeField, EWSElementField, Base64Field, \
    ItemField, IdField
from .properties import RootItemId, EWSElement
from .services import TNS, GetAttachment, CreateAttachment, DeleteAttachment

string_type = string_types[0]
log = logging.getLogger(__name__)


class AttachmentId(EWSElement):
    # 'id' and 'changekey' are UUIDs generated by Exchange
    # MSDN: https://msdn.microsoft.com/en-us/library/office/aa580987(v=exchg.150).aspx
    ELEMENT_NAME = 'AttachmentId'

    ID_ATTR = 'Id'
    ROOT_ID_ATTR = 'RootItemId'
    ROOT_CHANGEKEY_ATTR = 'RootItemChangeKey'
    FIELDS = [
        IdField('id', field_uri=ID_ATTR, is_required=True),
        IdField('root_id', field_uri=ROOT_ID_ATTR),
        IdField('root_changekey', field_uri=ROOT_CHANGEKEY_ATTR),
    ]

    __slots__ = ('id', 'root_id', 'root_changekey')


class Attachment(EWSElement):
    """
    Parent class for FileAttachment and ItemAttachment
    """
    FIELDS = [
        EWSElementField('attachment_id', value_cls=AttachmentId),
        TextField('name', field_uri='Name'),
        TextField('content_type', field_uri='ContentType'),
        TextField('content_id', field_uri='ContentId'),
        URIField('content_location', field_uri='ContentLocation'),
        IntegerField('size', field_uri='Size', is_read_only=True),  # Attachment size in bytes
        DateTimeField('last_modified_time', field_uri='LastModifiedTime'),
        BooleanField('is_inline', field_uri='IsInline'),
    ]

    __slots__ = ('parent_item', 'attachment_id', 'name', 'content_type', 'content_id', 'content_location', 'size',
                 'last_modified_time', 'is_inline')

    def __init__(self, **kwargs):
        self.parent_item = kwargs.pop('parent_item', None)
        super(Attachment, self).__init__(**kwargs)

    def clean(self, version=None):
        from .items import Item
        if self.parent_item is not None:
            assert isinstance(self.parent_item, Item)
        # pylint: disable=access-member-before-definition
        if self.content_type is None and self.name is not None:
            self.content_type = mimetypes.guess_type(self.name)[0] or 'application/octet-stream'
        super(Attachment, self).clean(version=version)

    def attach(self):
        # Adds this attachment to an item and updates the item_id and updated changekey on the parent item
        if self.attachment_id:
            raise ValueError('This attachment has already been created')
        if not self.parent_item or not self.parent_item.account:
            raise ValueError('Parent item %s must have an account' % self.parent_item)
        items = list(
            i if isinstance(i, Exception) else self.from_xml(elem=i, account=self.parent_item.account)
            for i in CreateAttachment(account=self.parent_item.account).call(parent_item=self.parent_item, items=[self])
        )
        assert len(items) == 1
        root_item_id = items[0]
        if isinstance(root_item_id, Exception):
            raise root_item_id
        attachment_id = root_item_id.attachment_id
        assert attachment_id.root_id == self.parent_item.item_id
        assert attachment_id.root_changekey != self.parent_item.changekey
        self.parent_item.changekey = attachment_id.root_changekey
        # EWS does not like receiving root_id and root_changekey on subsequent requests
        attachment_id.root_id = None
        attachment_id.root_changekey = None
        self.attachment_id = attachment_id

    def detach(self):
        # Deletes an attachment remotely and returns the item_id and updated changekey of the parent item
        if not self.attachment_id:
            raise ValueError('This attachment has not been created')
        if not self.parent_item or not self.parent_item.account:
            raise ValueError('Parent item %s must have an account' % self.parent_item)
        items = list(
            i if isinstance(i, Exception) else RootItemId.from_xml(elem=i, account=self.parent_item.account)
            for i in DeleteAttachment(account=self.parent_item.account).call(items=[self.attachment_id])
        )
        assert len(items) == 1
        root_item_id = items[0]
        if isinstance(root_item_id, Exception):
            raise root_item_id
        assert root_item_id.id == self.parent_item.item_id
        assert root_item_id.changekey != self.parent_item.changekey
        self.parent_item.changekey = root_item_id.changekey
        self.parent_item = None
        self.attachment_id = None

    def __hash__(self):
        if self.attachment_id is None:
            return hash(tuple(getattr(self, f) for f in self.__slots__[1:]))
        return hash(self.attachment_id)

    def __repr__(self):
        return self.__class__.__name__ + '(%s)' % ', '.join(
            '%s=%s' % (f.name, repr(getattr(self, f.name))) for f in self.FIELDS
            if f.name not in ('_item', '_content')
        )


class FileAttachment(Attachment):
    """
    MSDN: https://msdn.microsoft.com/en-us/library/office/aa580492(v=exchg.150).aspx
    """
    # TODO: This class is most likely inefficient for large data. Investigate methods to reduce copying
    ELEMENT_NAME = 'FileAttachment'
    FIELDS = Attachment.FIELDS + [
        BooleanField('is_contact_photo', field_uri='IsContactPhoto'),
        Base64Field('_content', field_uri='Content'),
    ]

    __slots__ = Attachment.__slots__ + ('is_contact_photo', '_content')

    def __init__(self, **kwargs):
        kwargs['_content'] = kwargs.pop('content', None)
        super(FileAttachment, self).__init__(**kwargs)

    @property
    def content(self):
        if self.attachment_id is None:
            return self._content
        if self._content is not None:
            return self._content
        # We have an ID to the data but still haven't called GetAttachment to get the actual data. Do that now.
        if not self.parent_item or not self.parent_item.account:
            raise ValueError('%s must have an account' % self.__class__.__name__)
        elems = list(GetAttachment(account=self.parent_item.account).call(
            items=[self.attachment_id], include_mime_content=False))
        assert len(elems) == 1
        elem = elems[0]
        if isinstance(elem, Exception):
            raise elem
        assert not isinstance(elem, tuple), elem
        # Don't use get_xml_attr() here because we want to handle empty file content as '', not None
        val = elem.find('{%s}Content' % TNS)
        if val is None:
            self._content = None
        else:
            self._content = base64.b64decode(val.text)
        elem.clear()
        return self._content

    @content.setter
    def content(self, value):
        assert isinstance(value, bytes)
        self._content = value

    @classmethod
    def from_xml(cls, elem, account):
        if elem is None:
            return None
        assert elem.tag == cls.response_tag(), (cls, elem.tag, cls.response_tag())
        kwargs = {f.name: f.from_xml(elem=elem, account=account) for f in cls.FIELDS}
        kwargs['content'] = kwargs.pop('_content')
        elem.clear()
        return cls(**kwargs)


class ItemAttachment(Attachment):
    """
    MSDN: https://msdn.microsoft.com/en-us/library/office/aa562997(v=exchg.150).aspx
    """
    ELEMENT_NAME = 'ItemAttachment'
    # noinspection PyTypeChecker
    FIELDS = Attachment.FIELDS + [
        ItemField('_item', field_uri='Item'),
    ]

    __slots__ = Attachment.__slots__ + ('_item',)

    def __init__(self, **kwargs):
        kwargs['_item'] = kwargs.pop('item', None)
        super(ItemAttachment, self).__init__(**kwargs)

    @property
    def item(self):
        if self.attachment_id is None:
            return self._item
        if self._item is not None:
            return self._item
        # We have an ID to the data but still haven't called GetAttachment to get the actual data. Do that now.
        if not self.parent_item or not self.parent_item.account:
            raise ValueError('%s must have an account' % self.__class__.__name__)
        items = list(
            i if isinstance(i, Exception) else self.__class__.from_xml(elem=i, account=self.parent_item.account)
            for i in GetAttachment(account=self.parent_item.account).call(
                items=[self.attachment_id], include_mime_content=True)
        )
        assert len(items) == 1
        attachment = items[0]
        if isinstance(attachment, Exception):
            raise attachment
        assert attachment.item is not None, 'GetAttachment returned no item'
        self._item = attachment.item
        return self._item

    @item.setter
    def item(self, value):
        from .items import Item
        assert isinstance(value, Item)
        self._item = value

    @classmethod
    def from_xml(cls, elem, account):
        if elem is None:
            return None
        assert elem.tag == cls.response_tag(), (cls, elem.tag, cls.response_tag())
        kwargs = {f.name: f.from_xml(elem=elem, account=account) for f in cls.FIELDS}
        kwargs['item'] = kwargs.pop('_item')
        elem.clear()
        return cls(**kwargs)
