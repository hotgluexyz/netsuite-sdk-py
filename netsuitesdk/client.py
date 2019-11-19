"""
    :class:`NetSuiteClient`: client proxy class which uses the python library
    Zeep to connect to a NetSuite account and make requests.
"""

import base64
import hashlib
import hmac
import logging
import os.path
import random
import time

from zeep import Client
from zeep.cache import SqliteCache
from zeep.transports import Transport
from zeep.exceptions import Fault
from zeep.exceptions import LookupError as ZeepLookupError

from .constants import *
from .exceptions import *
from .netsuite_types import *
from .utils import User, PaginatedSearch

class NetSuiteClient:
    """The Netsuite client class providing access to the Netsuite
    SOAP/WSDL web service"""

    WSDL_URL_TEMPLATE = 'https://{account}.suitetalk.api.netsuite.com/wsdl/v2017_2_0/netsuite.wsdl'

    DEFAULT_WSDL_URL = 'https://webservices.netsuite.com/wsdl/v2017_2_0/netsuite.wsdl'

    _search_preferences = None
    _passport = None
    _account = None

    # Used by TBA
    _consumer_key = None
    _consumer_secret = None
    _token_key = None
    _token_secret = None
    _app_id = None


    def __init__(self, account=None, wsdl_url=None, caching=True, caching_timeout=2592000):
        """
        Initialize the Zeep SOAP client, parse the xsd specifications
        of Netsuite and store the complex types as attributes of this
        instance.

        :param str account_id: Account ID to connect to
        :param str wsdl_url: WSDL url of the Netsuite SOAP service.
                            If None, defaults to DEFAULT_WSDL_URL
        :param str caching: If caching = 'sqlite', setup Sqlite caching
        :param int caching_timeout: Timeout in seconds for caching.
                            If None, defaults to 30 days
        """
        self.logger = logging.getLogger(self.__class__.__name__)
        self._account = account
        if wsdl_url:
            self._wsdl_url = wsdl_url
        else:
            if account:
                self._wsdl_url = self.WSDL_URL_TEMPLATE.format(account=account)
            else:
                self._wsdl_url = self.DEFAULT_WSDL_URL

        if caching:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache.db')
            timeout = caching_timeout
            cache = SqliteCache(path=path, timeout=timeout)
            transport = Transport(cache=cache)
        else:
            transport = None

        # Initialize the Zeep Client
        self._client = Client(self._wsdl_url, transport=transport)

        # Parse all complex types specified in :const:`~netsuitesdk.netsuite_types.COMPLEX_TYPES`
        # and store them as attributes of this instance. Same for simple types.
        self._namespaces = {}
        self._init_complex_types()
        self._init_simple_types()

        self._app_info = None
        self._is_authenticated = False
        self._user = None
        self.set_search_preferences()

    def set_search_preferences(self, body_fields_only: bool = True, page_size: int = 5, return_search_columns: bool = False):
        self._search_preferences = self.SearchPreferences(
            bodyFieldsOnly=body_fields_only,
            pageSize=page_size,
            returnSearchColumns=return_search_columns
        )

    def _init_complex_types(self):
        self._complex_types = {}
        for namespace, complex_types in COMPLEX_TYPES.items():
            if not namespace in self._namespaces:
                self._namespaces[namespace] = []
            for type_name in complex_types:
                try:
                    verbose_type_name = '{namespace}:{type_name}'.format(
                        namespace=namespace,
                        type_name=type_name
                    )
                    complex_type = self._client.get_type(verbose_type_name)
                except ZeepLookupError:
                    self.logger.warning('LookupError: Did not find complex type {}'.format(type_name))
                else:
                    setattr(self, type_name, complex_type)
                    self._complex_types[type_name] = complex_type
                    self._namespaces[namespace].append(complex_type)

    def _init_simple_types(self):
        self._simple_types = {}
        for namespace, simple_types in SIMPLE_TYPES.items():
            if not namespace in self._namespaces:
                self._namespaces[namespace] = []
            for type_name in simple_types:
                try:
                    verbose_type_name = '{namespace}:{type_name}'.format(
                        namespace=namespace,
                        type_name=type_name
                    )
                    simple_type = self._client.get_type(verbose_type_name)
                except ZeepLookupError:
                    self.logger.warning('LookupError: Did not find simple type {}'.format(type_name))
                else:
                    setattr(self, type_name, simple_type)
                    self._simple_types[type_name] = simple_type
                    self._namespaces[namespace].append(simple_type)

    def get_complex_type(self, type_name):
        # if ':' in type_name:
        #     namespace, type_name = type_name.split(':')
            # namespace_index = namespace[2:]
        return self._complex_types[type_name]

    def get_simple_type(self, type_name):
        return self._simple_types[type_name]

    def get_complex_type_attributes(self, complex_type):
        if isinstance(complex_type, str):
            complex_type = self.get_complex_type(complex_type)
        try:
            return [(attribute.name, attribute.type.name) for attribute in complex_type._attributes]
        except AttributeError:
            return []

    def get_complex_type_elements(self, complex_type):
        if isinstance(complex_type, str):
            complex_type = self.get_complex_type(complex_type)
        try:
            return [(attr_name, element.type.name) for attr_name, element in complex_type.elements]
        except AttributeError:
            return []

    def get_complex_type_info(self, complex_type):
        if isinstance(complex_type, str):
            complex_type = self.get_complex_type(complex_type)
            label = complex_type
        else:
            if hasattr(complex_type, 'name'):
                label = complex_type.name
            else:
                label = str(complex_type)
        attributes = self.get_complex_type_attributes(complex_type)
        elements = self.get_complex_type_elements(complex_type)
        yield 'complexType {}:'.format(label)
        if attributes:
            yield 'Attributes:'
            for name, type_name in attributes:
                yield '\t{}: {}'.format(name, type_name)
        else:
            yield 'No attributes'
        if elements:
            yield 'Elements:'
            for name, type_name in elements:
                yield '\t{}: {}'.format(name, type_name)
        else:
            yield 'No elements'

    def login(self, email, password, role, application_id):
        """
        Authenticate and login user for a Netsuite session. The passport argument is
        of type Passport(email, password, role and account) which holds the credentials
        and can be created with NetSuiteClient.create_password.

        :param int applicationId: All requests done in this session will be identified
            with this application id.
        :param Passport passport: holds the credentials to authenticate the user.
        :return: the login response which contains the response status and user roles
        :rtype: LoginResponse
        :raises :class:`~netsuitesdk.exceptions.NetSuiteLoginError`: if login was not successful. Possible codes
            are: InsufficientPermissionFault, InvalidAccountFault, InvalidSessionFault,
            InvalidCredentialsFault and UnexpectedErrorFault
        """

        role = self.RecordRef(internalId=role)
        self._passport = self.Passport(email=email, password=password, role=role, account=self._account)

        if self._is_authenticated:
            self.logout()
        try:
            self._app_info = self.ApplicationInfo(applicationId=application_id)
            response = self._client.service.login(
                                self._passport,
                                _soapheaders={'applicationInfo': self._app_info}
            )
            if response.status.isSuccess:
                self._is_authenticated = True
                logged_in_role = self._log_roles(response)
                self._user = User(name=response.userId['name'],
                                  internalId=response.userId['internalId'],
                                  wsRole=logged_in_role)
                self.logger.info("User {} logged in successfully.".format(str(self._user)))
                return response
            else:
                statusDetail = response.status['statusDetail'][0]
                exc = self._request_error('login',
                                          detail=statusDetail,
                                          error_cls=NetSuiteLoginError)
                raise exc
        except Fault as fault:
            exc = NetSuiteLoginError(str(fault), code=fault.code)
            self.logger.error(str(exc))
            raise exc from None

    def _generate_token_passport(self):
        def compute_nonce(length=20):
            """pseudo-random generated numeric string"""
            return ''.join([str(random.randint(0, 9)) for i in range(length)])

        nonce = compute_nonce(length=20)
        timestamp = int(time.time())
        key = '{}&{}'.format(self._consumer_secret, self._token_secret)
        base_string = '&'.join([self._account, self._consumer_key, self._token_key, nonce, str(timestamp)])
        key_bytes = key.encode(encoding='ascii')
        message_bytes = base_string.encode(encoding='ascii')
        # compute the signature
        if self._signature_algorithm == 'HMAC-SHA256':
            # hash
            hashed_value = hmac.new(key_bytes, msg=message_bytes, digestmod=hashlib.sha256)
        elif self._signature_algorithm == 'HMAC-SHA1':
            hashed_value = hmac.new(key_bytes, msg=message_bytes, digestmod=hashlib.sha1)
        else:
            raise NetSuiteError("signature_algorithm needs to be one of 'HMAC-SHA256', 'HMAC-SHA1'")

        dig = hashed_value.digest()
        # convert dig (a byte sequence) to a base 64 string
        value = base64.b64encode(dig).decode()

        signature = self.TokenPassportSignature(value, algorithm=self._signature_algorithm)
        return self.TokenPassport(account=self._account, consumerKey=self._consumer_key, token=self._token_key,
                                  nonce=nonce, timestamp=timestamp, signature=signature)


    def connect_tba(self, consumer_key, consumer_secret, token_key, token_secret, signature_algorithm='HMAC-SHA1'):
        """
        Create a TokenPassport object holding credentials for Token based
        authentication which will be passed to NetSuiteClient.login

        :param str account: the NetSuite account ID
        :param str consumer_key: the consumer key for the integration record
        :param str consumer_secret: the consumer secret
        :param str token_key: a string identifier of a token representing a
                        unique combination of a user, a role and an integration record
        :param str token_secret: the token secret
        :param str signature_algorithm: algorithm to compute the signature value (a hashed value),
                        choices are 'HMAC-SHA256' or 'HMAC-SHA1'
        """
        self._consumer_key = consumer_key
        self._consumer_secret = consumer_secret
        self._token_key = token_key
        self._token_secret = token_secret
        self._signature_algorithm = signature_algorithm

    def _log_roles(self, response):
        roles = response.wsRoleList['wsRole']
        self._roles = []
        logged_in_role = None
        for role in roles:
            record_ref = self.RecordRef(name=role['role'].name,
                                        internalId=role['role'].internalId)
            wsRole = self.WsRole(
                        role=record_ref,
                        isDefault=role['isDefault'],
                        isInactive=role['isInactive'],
                        isLoggedInRole=role['isLoggedInRole']
                    )
            self._roles.append(wsRole)
            if wsRole.isLoggedInRole:
                logged_in_role = wsRole
        self.logger.info('There are {} user roles: {}'.format(len(self._roles),
            ', '.join(['{}({})'.format(role.role.name, role.role.internalId) for role in self._roles])))
        return logged_in_role

    @property
    def logged_in(self):
        return self._is_authenticated

    def logout(self):
        if not self._is_authenticated:
            return
        response = self._client.service.logout()
        self._is_authenticated = False
        self.logger.info("User {user} was logged out.".format(user=str(self._user)))
        self._user = None
        return response.status

    def _request_error(self, service_name, detail, error_cls=None):
        if error_cls is None:
            error_cls = NetSuiteRequestError
        exc = error_cls(
                "An error occured in a {service_name} request: {msg}".format(
                                                    service_name=service_name,
                                                    msg=detail['message']),
                code=detail['code']
        )
        self.logger.error(str(exc))
        return exc

    def _build_soap_headers(self, include_search_preferences: bool = False):
        """
        Generate soap headers dictionary to send with a request

        :param Passport passport: holds the authentication credentials
        :param TokenPassport tokenPassport: holds the token based authentication details
        :param ApplicationInfo applicationInfo: contains the application Id
        :return: the dictionary representing the headers
        :rtype: dict
        :raises :class:`~netsuitesdk.exceptions.NetSuiteError`: if user is neither logged in nor a passport or tokenPassport was passed
        """

        soapheaders = {}

        if self._is_authenticated:
            # User is already logged in, so there is no
            # need to pass authentication details in the header
            pass
        elif self._consumer_key is not None:
            soapheaders['tokenPassport'] = self._generate_token_passport()
        elif self._passport is not None:
            soapheaders['passport'] = self._passport
        else:
            raise NetSuiteError('Must either login first or pass passport or tokenPassport to request header.')
        if include_search_preferences:
            soapheaders['searchPreferences'] = self._search_preferences

        return soapheaders

    def request(self, name, *args, **kwargs):
        """
        Make a NetSuite web service request

        :param str name: the name of the request service ('get', 'search', ...)
        :return: the request response object
        :rtype: the exact type depends on the request
        """
        service = getattr(self._client.service, name)
        # call the service:
        include_search_preferences = (name == 'search')
        response = service(*args, 
                _soapheaders=self._build_soap_headers(include_search_preferences=include_search_preferences)
                , **kwargs)
        return response

    def get(self, recordType, internalId=None, externalId=None):
        """
        Make a get request to retrieve an object of type recordType
        specified by either internalId or externalId

        :param str recordType: the complex type (e.g. 'vendor')
        :param int internalId: id specifying the record to be retrieved
        :param str externalId: str specifying the record to be retrieved
        :return: the matching record in case of success
        :rtype: Record
        :raises ValueError: if neither internalId nor externalId was passed
        """

        recordType = recordType[0].lower() + recordType[1:]
        if internalId is not None:
            record_ref = self.RecordRef(type=recordType, internalId=internalId)
        elif externalId is not None:
            record_ref = self.RecordRef(type=recordType, externalId=externalId)
        else:
            raise ValueError('Either internalId or externalId is necessary to make a get request.')
        response = self.request('get', baseRef=record_ref)
        response = response.body.readResponse

        status = response.status
        if status.isSuccess:
            record = response['record']
            return record
        else:
            exc = self._request_error('get', detail=status['statusDetail'][0])
            raise exc

    def getAll(self, recordType):
        """
        Make a getAll request to retrieve all objects of type recordType.
        All NetSuite types available for a search
        are listed under :const:`constants.GET_ALL_RECORD_TYPES`.

        :param str recordType: the complex type (e.g. 'vendor')
        :param int internalId: id specifying the record to be retrieved
        :param str externalId: str specifying the record to be retrieved
        :return: the matching record in case of success
        :rtype: Record
        """

        recordType = recordType[0].lower() + recordType[1:]
        record = self.GetAllRecord(recordType=recordType)
        response = self.request('getAll', record=record)
        response = response.body.getAllResult

        status = response.status
        if status.isSuccess:
            records = response['recordList']['record']
            return records
        else:
            exc = self._request_error('getAll', detail=status['statusDetail'][0])
            raise exc

    def search_factory(self, type_name, **kwargs):
        _type_name = type_name[0].lower() + type_name[1:]
        if not _type_name in SEARCH_RECORD_TYPES:
            raise NetSuiteTypeError('{} is not a searchable NetSuite type!'.format(type_name))
        search_cls_name = '{}Search'.format(type_name)
        search_cls = self.get_complex_type(search_cls_name)
        search_record = search_cls(**kwargs)
        return search_record

    def basic_search_factory(self, type_name, **kwargs):
        _type_name = type_name[0].lower() + type_name[1:]
        if not _type_name in SEARCH_RECORD_TYPES:
            raise NetSuiteTypeError('{} is not a searchable NetSuite type!'.format(type_name))
        basic_search_cls_name = '{}SearchBasic'.format(type_name)
        basic_search_cls = self.get_complex_type(basic_search_cls_name)
        basic_search = basic_search_cls()
        for key, value in kwargs.items():
            setattr(basic_search, key, value)
        return basic_search

    def search(self, searchRecord):
        """
        Make a search request to retrieve an object of type recordType
        specified by internalId. All NetSuite types available for a search
        are listed under :const:`constants.SEARCH_RECORD_TYPES`.

        :param Record searchRecord: data object holding all parameters for the search.
                    The utility function `search_factory` can be used to create one.
        :return: result records and meta data about search result
        :rtype: SearchResult(type):
                    int totalRecords: total number of records
                    int pageSize: number of records per page
                    int totalPages: number of pages
                    int pageIndex: index of actual returned result page
                    str searchId: identifier for the search
                    list records: the actual records found
        """
        response = self.request('search',
                                searchRecord=searchRecord)

        result = response.body.searchResult
        status = result.status
        success = status.isSuccess
        if success:
            if hasattr(result.recordList, 'record'):
                result.records = result.recordList.record
                return result
            else:
                # Did not find anything
                result.records = None
                return result
        else:
            exc = self._request_error('search', detail=status['statusDetail'][0])
            raise exc

    def searchMoreWithId(self, searchId, pageIndex):
        response = self.request('searchMoreWithId',
                                searchId=searchId,
                                pageIndex=pageIndex)

        result = response.body.searchResult
        status = result.status
        success = status.isSuccess
        if success:
            result.records = result.recordList.record
            return result
        else:
            exc = self._request_error('searchMoreWithId', detail=status['statusDetail'][0])
            raise exc

    def upsert(self, record):
        """
        Add an object of type recordType with given externalId..
        If a record of specified type with matching externalId already
        exists, it is updated.

        Usage example:
            customer = self.Customer()
            customer.externalId = 'customer_id'
            customer.companyName = 'Test Inc.'
            customer.email = 'test@example.com'
            self.upsert(record=customer)

        :param str recordType: the complex type (e.g. either 'Customer' or 'vendors')
        :param str externalId: str specifying the record to be retrieved
        :return: a reference to the newly created or updated record (in case of success)
        :rtype: RecordRef
        """

        response = self.request('upsert', record=record)
        response = response.body.writeResponse
        status = response.status
        if status.isSuccess:
            record_ref = response['baseRef']
            self.logger.info('Successfully updated record of type {type}, internalId: {internalId}, externalId: {externalId}'.format(
                    type=record_ref['type'], internalId=record_ref['internalId'], externalId=record_ref['externalId']))
            return record_ref
        else:
            exc = self._request_error('upsert', detail=status['statusDetail'][0])
            raise exc

    def upsertList(self, records):
        """
        Add objects of type recordType with given externalId..
        If a record of specified type with matching externalId already
        exists, it is updated.

        Usage example:
            customer1 = self.Customer(externalId='customer', email='test1@example.com')
            customer2 = self.Customer(externalId='another_customer', email='test2@example.com')
            self.upsertList(records=[customer1, customer2])

        :param list[CompoundValue] records: the records to be created or updated
        :return: a reference to the newly created or updated records
        :rtype: list[CompoundValue]
        """

        response = self.request('upsertList', record=records)
        responses = response.body.writeResponse
        has_failures = False
        record_refs = []
        for response in responses:
            status = response.status
            if status.isSuccess:
                record_ref = response['baseRef']
                self.logger.info('Successfully updated record of type {type}, internalId: {internalId}, externalId: {externalId}'.format(
                        type=record_ref['type'], internalId=record_ref['internalId'], externalId=record_ref['externalId']))
                record_refs.append(record_ref)
            else:
                exc = self._request_error('upsertList', detail=status['statusDetail'][0])
                has_failures = True
                raise exc
        return record_refs

    ######## Utility functions ########

    def to_json(self, record, include_none_values=False):
        """ Convert a netsuite record (e.g. as
        returned from `get`) into a dictionary """

        if include_none_values:
            return record.__values__
        values = {}
        for name, value in record.__values__.items():
            if value is not None:
                values[name] = value
        return values

    def print_values(self, record, include_none_values=False):
        """
        :param CompoundValue record: the record whose values should be printed
        :param bool include_none_values: if True, also None values will be printed
        """

        for key, value in self.to_json(record, include_none_values=include_none_values).items():
            print('{}: {}'.format(key, str(value)))

    def print_records(self, records, print_func=None):
        if print_func is None:
            print_func = self.print_values
        for record in records:
            print_func(record)
            print('-'*15)

    def paginated_search(self, type_name, basic_search=None, page_size=None, print_func=None):
        """
        Uses `PaginatedSearch` (defined in utils.py) which in turn uses
        `NetSuiteClient.search` to perform a search on the NetSuite type `type_name`
        and returns a PaginatedSearch object containing the search results in
        its attribute `records`.

        :param str type_name: name of the type the search is performed on, e.g. 'Vendor'
        :param BasicSearch basic_search: set this to perform basic filtering
        """

        paginated_search = PaginatedSearch(client=self,
                                           type_name=type_name,
                                           basic_search=basic_search,
                                           page_size=page_size,
                                           )
        print('totalRecords: ', paginated_search.total_records)
        print('pageSize: ', paginated_search.page_size)
        print('totalPages: ', paginated_search.total_pages)
        print('pageIndex: ', paginated_search.page_index)
        print('results on page: ', paginated_search.num_records)
        if print_func is None: print_func = self.print_values
        self.print_records(records=paginated_search.records, print_func=print_func)
        for i in range(2, paginated_search.total_pages):
            inp = input('q: quit this view, any other key: next page\n')
            if inp == 'q':
                break
            paginated_search.goto_page(page_index=i)
            self.print_records(records=paginated_search.records, print_func=print_func)

    def basic_stringfield_search(self, type_name, attribute, value, operator=None):
        """
        Searches for an object of type `type_name` whose name contains `value`

        :param str type_name: the name of the NetSuite type to be searched in
        :param str attribute: the attribute of the type to be used for the search
        :param str value: the value to be used for the search
        :param str operator: mode used to search for value, possible:
                    'is', 'contains', 'doesNotContain',
                    'doesNotStartWith', 'empty', 'hasKeywords',
                    'isNot', 'notEmpty', 'startsWith'

        See for example: http://www.netsuite.com/help/helpcenter/en_US/srbrowser/Browser2017_2/schema/search/locationsearchbasic.html?mode=package
        In general, one can find the possible search attributes for a basic search
        in the type {type_name}SearchBasic
        """

        search_cls_name = '{type_name}SearchBasic'.format(type_name=type_name)
        search_cls = getattr(self, search_cls_name)
        if operator is None: operator = 'is'
        string_field = self.SearchStringField(
                                    searchValue=value,
                                    operator=operator)
        basic_search = search_cls()
        setattr(basic_search, attribute, string_field)
        result = self.search(basic_search)
        if result.records:
            return result.records
        else:
            #print('Did not find {type}, {attribute} {operator} {value}'.format(
            #        type=type_name, attribute=attribute, operator=operator, value=value))
            return None