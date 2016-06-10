from dateutil.parser import parse
import os
import requests
from requests.exceptions import HTTPError
from tracking.models import DepartmentUser
from tracking.utils import logger_setup


FRESHDESK_ENDPOINT = os.environ['FRESHDESK_ENDPOINT']
FRESHDESK_AUTH = (os.environ['FRESHDESK_KEY'], 'X')
HEADERS_JSON = {'Content-Type': 'application/json'}


def get_freshdesk_object(obj_type, id):
    """Query the Freshdesk v2 API for a single object.
    """
    url = FRESHDESK_ENDPOINT + '/{}/{}'.format(obj_type, id)
    r = requests.get(url, auth=FRESHDESK_AUTH)
    if not r.status_code == 200:
        r.raise_for_status()
    return r.json()


def update_freshdesk_object(obj_type, data, id=None):
    """Use the Freshdesk v2 API to create or update an object.
    Accepts a dict of fields.
    Ref: https://developer.freshdesk.com/api/#create_contact
    """
    if not id:  # Assume creation of new object.
        url = FRESHDESK_ENDPOINT + '/{}'.format(obj_type)
        r = requests.post(url, auth=FRESHDESK_AUTH, data=data)
    else:
        url = FRESHDESK_ENDPOINT + '/{}/{}'.format(obj_type, id)
        r = requests.put(url, auth=FRESHDESK_AUTH, data=data)
    return r  # Return the response, so we can handle non-200 gracefully.


def get_freshdesk_objects(obj_type, progress=True, limit=False):
    """Query the Freshdesk v2 API for all objects of a defined type.
    ``limit`` should be an integer (maximum number of objects to return).
    May take some time, depending on the number of objects.
    """
    url = FRESHDESK_ENDPOINT + '/{}'.format(obj_type)
    params = {'page': 1, 'per_page': 100}
    objects = []
    further_results = True

    while further_results:
        if progress:
            print('Querying page {}'.format(params['page']))

        r = requests.get(url, auth=FRESHDESK_AUTH, params=params)
        if not r.status_code == 200:
            r.raise_for_status()

        if 'link' not in r.headers:  # No further paginated results.
            further_results = False
            if progress:
                print('Done!')

        objects.extend(r.json())
        params['page'] += 1

        if limit and len(objects) >= limit:
            further_results = False
            objects = objects[:limit]

    # Return the full list of objects returned.
    return objects


def freshdesk_sync_contacts(contacts=None, companies=None, agents=None):
    """Iterate through all DepartmentUser objects, and ensure that each user's
    information is synced correctly to a Freshdesk contact.
    May optionally be passed in dicts of contacts & companies.
    """
    logger = logger_setup('freshdesk_sync_contacts')

    try:
        if not contacts:
            logger.info('Querying Freshdesk for current contacts')
            contacts = get_freshdesk_objects(obj_type='contacts', progress=False)
            contacts = {c['email'].lower(): c for c in contacts if c['email']}
        if not companies:
            logger.info('Querying Freshdesk for current companies')
            companies = get_freshdesk_objects(obj_type='companies', progress=False)
            companies = {c['name']: c for c in companies}
        if not agents:
            logger.info('Querying Freshdesk for current agents')
            agents = get_freshdesk_objects(obj_type='agents', progress=False)
            agents = {a['contact']['email'].lower(): a['contact'] for a in agents if a['contact']['email']}
    except Exception as e:
        logger.exception(e)
        return False

    # Filter DepartmentUsers: valid email (contains @), not -admin, DN contains 'OU=Users'
    for user in DepartmentUser.objects.filter(email__contains='@', ad_dn__contains='OU=Users').exclude(email__contains='-admin'):
        if user.email.lower() in contacts:
            # The DepartmentUser exists in Freshdesk; verify and update details.
            fd = contacts[user.email.lower()]
            data = {}
            user_sync = False
            # use extra attributes from org_data, if available
            cost_centre = user.org_data.get('cost_centre', {}).get('code', "") if user.org_data else ""
            physical_location = user.org_data.get('location', {}).get('name', "") if user.org_data else ""
            department = user.org_data.get('units', []) if user.org_data else []
            department = department[0].get('name', "") if len(department) > 0 else ""
            changes = []

            if user.name != fd['name']:
                user_sync = True
                data['name'] = user.name
                changes.append('name')
            if user.telephone != fd['phone']:
                user_sync = True
                data['phone'] = user.telephone
                changes.append('phone')
            if user.title != fd['job_title']:
                user_sync = True
                data['job_title'] = user.title
                changes.append('job_title')
            if department and department in companies and fd['company_id'] != companies[department]['id']:
                user_sync = True
                data['company_id'] = companies[department]['id']
                changes.append('company_id')
            # Custom fields in Freshdesk: Cost Centre no.
            if fd['custom_field']['cf_cost_centre'] != cost_centre:
                user_sync = True
                data['custom_field'] = {'cf_cost_centre': cost_centre}
                changes.append('cost_centre')
            # Custom fields in Freshdesk: Physical location
            if fd['custom_field']['cf_location'] != physical_location:
                user_sync = True
                if 'custom_field' in data:
                    data['custom_field']['cf_location'] = physical_location
                else:
                    data['custom_field'] = {'cf_location': physical_location}
                changes.append('physical_location')
            if user_sync:  # Sync user details to their Freshdesk contact.
                r = update_freshdesk_object(data, obj_type='contacts', id=fd['id'])
                if r.status_code == 403:  # Forbidden
                    # A 403 response probably means that we hit the API throttle limit.
                    # Abort the synchronisation.
                    logger.error('HTTP403 received from Freshdesk API, aborting')
                    return False
                logger.info('{} was updated in Freshdesk (status {}), changed: {}'.format(
                    user.email.lower(), r.status_code, ', '.join(changes)))
            else:
                logger.info('{} already up to date in Freshdesk'.format(user.email.lower()))
        elif user.email.lower() in agents:
            # The DepartmentUser is an agent; skip (can't update Agent details via the API).
            logger.info('{} is an agent, skipping sync'.format(user.email.lower()))
            continue
        else:
            # The DepartmentUser does not exist in Freshdesk; create them as a Contact.
            data = {'name': user.name, 'email': user.email.lower(),
                    'phone': user.telephone, 'job_title': user.title}
            if department and department in companies:
                data['company_id'] = companies[department]['id']
            r = update_freshdesk_object(data, obj_type='contacts')
            if not r.status_code == 200:  # Error, unable to process request.
                logger.warn('{} not created in Freshdesk (status {})'.format(user.email.lower(), r.status_code))
            else:
                logger.info('{} created in Freshdesk (status {})'.format(user.email.lower(), r.status_code))

    return True


def freshdesk_cache_tickets(tickets=None):
    """Cache passed-in list of Freshdesk tickets in the database. If no tickets
    are passed in, query the API for the newest tickets.
    """
    from registers.models import FreshdeskTicket, FreshdeskConversation, FreshdeskContact
    logger = logger_setup('freshdesk_cache_tickets')

    if not tickets:
        try:
            logger.info('Querying Freshdesk for current tickets')
            tickets = get_freshdesk_objects(obj_type='tickets', progress=False, limit=30)
            for t in tickets:
                # Rename key 'id'.
                t['ticket_id'] = t.pop('id')
                # Date ISO8601-formatted date strings into datetimes.
                t['created_at'] = parse(t['created_at'])
                t['due_by'] = parse(t['due_by'])
                t['fr_due_by'] = parse(t['fr_due_by'])
                t['updated_at'] = parse(t['updated_at'])
                # Pop unused fields from the dict.
                t.pop('company_id')
                t.pop('email_config_id')
                t.pop('product_id')
        except Exception as e:
            logger.exception(e)
            return False

    created, updated = 0, 0
    # Iterate through tickets; determine if a cached FreshdeskTicket should be
    # created or updated.
    for t in tickets:
        try:
            ft, create = FreshdeskTicket.objects.update_or_create(ticket_id=t['ticket_id'], defaults=t)
            if create:
                logger.info('{} created'.format(ft))
                created += 1
            else:
                logger.info('{} updated'.format(ft))
                updated += 1
            # Sync contact objects (requester and responder).
            # Check local cache first, to reduce no of API calls.
            if ft.requester_id:
                if FreshdeskContact.objects.filter(contact_id=ft.requester_id).exists():
                    ft.freshdesk_requester = FreshdeskContact.objects.get(contact_id=ft.requester_id)
                else:  # Attempt to cache the Freshdesk contact.
                    try:
                        c = get_freshdesk_object(obj_type='contacts', id=ft.requester_id)
                        c['contact_id'] = c.pop('id')
                        c['created_at'] = parse(c['created_at'])
                        c['updated_at'] = parse(c['updated_at'])
                        c.pop('avatar')
                        c.pop('company_id')
                        c.pop('twitter_id')
                        con = FreshdeskContact.objects.create(**c)
                        logger.info('Created {}'.format(con))
                        ft.freshdesk_requester = con
                    except HTTPError:  # The GET might fail if the contact is an agent.
                        logger.error('HTTP 404 Freshdesk contact not found: {}'.format(ft.requester_id))
                        pass
            if ft.responder_id:
                if FreshdeskContact.objects.filter(contact_id=ft.responder_id).exists():
                    ft.freshdesk_responder = FreshdeskContact.objects.get(contact_id=ft.responder_id)
                else:  # Attempt to cache the Freshdesk contact.
                    try:
                        c = get_freshdesk_object(obj_type='contacts', id=ft.responder_id)
                        c['contact_id'] = c.pop('id')
                        c['created_at'] = parse(c['created_at'])
                        c['updated_at'] = parse(c['updated_at'])
                        c.pop('avatar')
                        c.pop('company_id')
                        c.pop('twitter_id')
                        con = FreshdeskContact.objects.create(**c)
                        logger.info('Created {}'.format(con))
                        ft.freshdesk_responder = con
                    except HTTPError:  # The GET might fail if the contact is an agent.
                        logger.error('HTTP 404 Freshdesk contact not found: {}'.format(ft.responder_id))
                        pass
            ft.save()

            # Sync ticket conversation objects.
            obj = 'tickets/{}/conversations'.format(t['ticket_id'])
            convs = get_freshdesk_objects(obj_type=obj, progress=False)
            for c in convs:
                # Rename key 'id'.
                c['conversation_id'] = c.pop('id')
                # Date ISO8601-formatted date strings into datetimes.
                c['created_at'] = parse(c['created_at'])
                c['updated_at'] = parse(c['updated_at'])
                # Pop unused fields from the dict.
                c.pop('bcc_emails')
                c.pop('support_email')
                fc, create = FreshdeskConversation.objects.update_or_create(conversation_id=c['conversation_id'], defaults=c)
                if create:
                    logger.info('{} created'.format(fc))
                else:
                    logger.info('{} updated'.format(fc))
                # Link parent ticket, DepartmentUser, etc.
                fc.freshdesk_ticket = ft
                if FreshdeskContact.objects.filter(contact_id=fc.user_id).exists():
                    fc.freshdesk_contact = FreshdeskContact.objects.get(contact_id=fc.user_id)
                else:  # Attempt to cache the Freshdesk contact.
                    try:
                        f_con = get_freshdesk_object(obj_type='contacts', id=fc.user_id)
                        f_con['contact_id'] = f_con.pop('id')
                        f_con['created_at'] = parse(f_con['created_at'])
                        f_con['updated_at'] = parse(f_con['updated_at'])
                        f_con.pop('avatar')
                        f_con.pop('company_id')
                        f_con.pop('twitter_id')
                        contact = FreshdeskContact.objects.create(**f_con)
                        logger.info('Created {}'.format(contact))
                        fc.freshdesk_contact = contact
                    except HTTPError:  # The GET might fail if the contact is an agent.
                        logger.error('HTTP 404 Freshdesk contact not found: {}'.format(ft.requester_id))
                        pass
                fc.save()
        except Exception as e:
            logger.exception(e)
            return False

    logger.info('Ticket sync: {} created, {} updated'.format(created, updated))
    print('{} created, {} updated'.format(created, updated))

    return True
