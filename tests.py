import logging
import string
import random
import datetime
from dateutil.relativedelta import relativedelta
from dateutil import rrule

from django.test import TransactionTestCase, TestCase
from django.contrib.auth.models import User
from django.core.urlresolvers import reverse

from rapidsms.tests.harness import MockRouter, MockBackend
from rapidsms.models import Connection, Contact, Backend
from rapidsms.messages.outgoing import OutgoingMessage
from rapidsms.messages.incoming import IncomingMessage
from rapidsms.tests.scripted import TestScript

from afrims.tests.testcases import CreateDataTest
from afrims.apps.broadcast.models import Broadcast, DateAttribute
from afrims.apps.broadcast.app import BroadcastApp, scheduler_callback
from afrims.apps.broadcast.forms import BroadcastForm


class BroadcastCreateDataTest(CreateDataTest):
    """ Base test case that provides helper functions to create data """

    def create_broadcast(self, when='', commit=True, data={}):
        now = datetime.datetime.now()
        defaults = {
            'date': now,
            'schedule_frequency': 'daily',
            'body': self.random_string(160),
        }
        defaults.update(data)
        groups = defaults.pop('groups', [])
        weekdays = defaults.pop('weekdays', [])
        months = defaults.pop('months', [])
        # simple helper flag to create broadcasts in the past or future
        delta = relativedelta(days=1)
        if when == 'ready':
            defaults.update({'date': now - delta})
        elif when == 'future':
            defaults.update({'date': now + delta})
        broadcast = Broadcast.objects.create(**defaults)
        if groups:
            broadcast.groups = groups
        if weekdays:
            broadcast.weekdays = weekdays
        if months:
            broadcast.months = months
        return broadcast

    def get_weekday(self, day):
        return DateAttribute.objects.get(name__iexact=day,
                                         type__exact='weekday')

    def get_weekday_for_date(self, date):
        return DateAttribute.objects.get(value=date.weekday(),
                                         type__exact='weekday')

    def get_month(self, day):
        return DateAttribute.objects.get(name__iexact=day, type__exact='month')

    def get_month_for_date(self, date):
        return DateAttribute.objects.get(value=date.month, type__exact='month')

    def assertDateEqual(self, date1, date2):
        """ date comparison that ignores microseconds """
        date1 = date1.replace(microsecond=0)
        date2 = date2.replace(microsecond=0)
        self.assertEqual(date1, date2)


class DateAttributeTest(BroadcastCreateDataTest):
    """ Test pre-defined data in initial_data.json against rrule constants """

    def test_weekdays(self):
        """ Test weekdays match """
        self.assertEqual(self.get_weekday('monday').value, rrule.MO.weekday)
        self.assertEqual(self.get_weekday('tuesday').value, rrule.TU.weekday)
        self.assertEqual(self.get_weekday('wednesday').value, rrule.WE.weekday)
        self.assertEqual(self.get_weekday('thursday').value, rrule.TH.weekday)
        self.assertEqual(self.get_weekday('friday').value, rrule.FR.weekday)
        self.assertEqual(self.get_weekday('saturday').value, rrule.SA.weekday)
        self.assertEqual(self.get_weekday('sunday').value, rrule.SU.weekday)


class BroadcastDateTest(BroadcastCreateDataTest):

    def test_get_next_date_future(self):
        """ get_next_date shouln't increment if date is in the future """
        date = datetime.datetime.now() + relativedelta(hours=1)
        broadcast = self.create_broadcast(data={'date': date})
        self.assertEqual(broadcast.get_next_date(), date)
        broadcast.set_next_date()
        self.assertEqual(broadcast.date, date,
                         "set_next_date should do nothing")

    def test_get_next_date_past(self):
        """ get_next_date should increment if date is in the past """
        date = datetime.datetime.now() - relativedelta(hours=1)
        broadcast = self.create_broadcast(data={'date': date})
        self.assertTrue(broadcast.get_next_date() > date)
        broadcast.set_next_date()
        self.assertTrue(broadcast.date > date,
                        "set_next_date should increment date")

    def test_one_time_broadcast(self):
        """ one-time broadcasts should disable and not increment """
        date = datetime.datetime.now()
        data = {'date': date, 'schedule_frequency': 'one-time'}
        broadcast = self.create_broadcast(data=data)
        self.assertEqual(broadcast.get_next_date(), None,
                         "one-time broadcasts have no next date")
        broadcast.set_next_date()
        self.assertEqual(broadcast.date, date,
                         "set_next_date shoudn't change date of one-time")
        self.assertEqual(broadcast.schedule_frequency, None,
                         "set_next_date should disable one-time")

    def test_by_weekday_yesterday(self):
        """ Test weekday recurrences for past day """
        yesterday = datetime.datetime.now() - relativedelta(days=1)
        data = {'date': yesterday, 'schedule_frequency': 'weekly',
                'weekdays': [self.get_weekday_for_date(yesterday)]}
        broadcast = self.create_broadcast(data=data)
        next = yesterday + relativedelta(weeks=1)
        self.assertDateEqual(broadcast.get_next_date(), next)

    def test_by_weekday_tomorrow(self):
        """ Test weekday recurrences for future day (shouldn't change) """
        tomorrow = datetime.datetime.now() + relativedelta(days=1)
        data = {'date': tomorrow, 'schedule_frequency': 'weekly',
                'weekdays': [self.get_weekday_for_date(tomorrow)]}
        broadcast = self.create_broadcast(data=data)
        self.assertDateEqual(broadcast.get_next_date(), tomorrow)

    def test_end_date_disable(self):
        """ Broadcast should disable once end date is reached """
        broadcast = self.create_broadcast(when='ready')
        broadcast.schedule_end_date = datetime.datetime.now()
        self.assertEqual(broadcast.get_next_date(), None)

    def test_month_recurrence(self):
        """ Test monthly recurrence for past month """
        day = datetime.datetime.now() + relativedelta(days=1)
        one_month_ago = day - relativedelta(months=1)
        data = {'date': one_month_ago, 'schedule_frequency': 'monthly'}
        broadcast = self.create_broadcast(data=data)
        self.assertDateEqual(broadcast.get_next_date(), day)

    def test_bymonth_recurrence(self):
        """ Test bymonth recurrence for past month """
        day = datetime.datetime.now() + relativedelta(days=1)
        one_month_ago = day - relativedelta(months=1)
        next_month = day + relativedelta(months=1)
        months = (self.get_month_for_date(one_month_ago),
                  self.get_month_for_date(next_month))
        data = {'date': one_month_ago, 'schedule_frequency': 'monthly',
                'months': months}
        broadcast = self.create_broadcast(data=data)
        self.assertDateEqual(broadcast.get_next_date(), next_month)


class BroadcastAppTest(BroadcastCreateDataTest):

    def test_queue_creation(self):
        """ Test broadcast messages are queued properly """
        c1 = self.create_contact()
        g1 = self.create_group()
        c1.groups.add(g1)
        c2 = self.create_contact()
        g2 = self.create_group()
        c2.groups.add(g2)
        broadcast = self.create_broadcast(data={'groups': [g1]})
        broadcast.queue_outgoing_messages()
        self.assertEqual(broadcast.messages.count(), 1)
        contacts = broadcast.messages.values_list('recipient', flat=True)
        self.assertTrue(c1.pk in contacts)
        self.assertFalse(c2.pk in contacts)

    def test_ready_manager(self):
        """ test Broadcast.ready manager returns broadcasts ready to go out """
        b1 = self.create_broadcast(when='past')
        b2 = self.create_broadcast(when='future')
        ready = Broadcast.ready.values_list('id', flat=True)
        self.assertTrue(b1.pk in ready)
        self.assertFalse(b2.pk in ready)


class BroadcastFormTest(BroadcastCreateDataTest):
    def setUp(self):
        self.contact = self.create_contact()
        self.group = self.create_group()
        self.contact.groups.add(self.group)

    def test_future_start_date_required(self):
        """ Start date is required for future broadcasts """
        data =  {
            'when': 'later',
            'body': self.random_string(160),
            'schedule_frequency': 'one-time',
            'groups': [self.group.pk],
        }
        form = BroadcastForm(data)
        self.assertFalse(form.is_valid())
        self.assertEqual(len(form.non_field_errors()), 1)
        msg = 'Start date is required for future broadcasts'
        self.assertTrue(msg in form.non_field_errors().as_text())

    def test_now_date_set_on_save(self):
        """ 'now' messages automatically get date assignment """
        data =  {
            'when': 'now',
            'body': self.random_string(160),
            'groups': [self.group.pk],
        }
        form = BroadcastForm(data)
        self.assertTrue(form.is_valid())
        broadcast = form.save()
        self.assertTrue(broadcast.date is not None)

    def test_end_date_before_start_date(self):
        """ Form should prevent end date being before start date """
        now = datetime.datetime.now()
        yesterday = now - relativedelta(days=1)
        data =  {
            'when': 'later',
            'body': self.random_string(160),
            'date': now,
            'schedule_end_date': yesterday,
            'schedule_frequency': 'daily',
            'groups': [self.group.pk],
        }
        form = BroadcastForm(data)
        self.assertFalse(form.is_valid())
        self.assertEqual(len(form.non_field_errors()), 1)
        msg = 'End date must be later than start date'
        self.assertTrue(msg in form.non_field_errors().as_text())

    def test_update(self):
        """ Test broadcast edit functionality """
        before = self.create_broadcast(when='future',
                                       data={'groups': [self.group.pk]})
        data =  {
            'when': 'later',
            'date': before.date,
            'body': self.random_string(30),
            'schedule_frequency': before.schedule_frequency,
            'groups': [self.group.pk],
        }
        form = BroadcastForm(data, instance=before)
        self.assertTrue(form.is_valid(), form._errors.as_text())
        before = Broadcast.objects.get(pk=before.pk)
        after = form.save()
        # same broadcast
        self.assertEqual(before.pk, after.pk)
        # new message
        self.assertNotEqual(before.body, after.body)

    def test_field_clearing(self):
        """ Non related frequency fields should be cleared on form clean """
        weekday = self.get_weekday_for_date(datetime.datetime.now())
        before = self.create_broadcast(when='future',
                                       data={'groups': [self.group.pk],
                                             'weekdays': [weekday]})
        data =  {
            'when': 'later',
            'date': before.date,
            'body': before.body,
            'schedule_frequency': 'monthly',
            'groups': [self.group.pk],
            'weekdays': [weekday.pk],
        }
        form = BroadcastForm(data, instance=before)
        after = form.save()
        self.assertEqual(after.weekdays.count(), 0)


class BroadcastViewTest(BroadcastCreateDataTest):
    def setUp(self):
        self.user = User.objects.create_user('test', 'a@b.com', 'abc')
        self.user.save()
        self.client.login(username='test', password='abc')

    def test_delete(self):
        """ Make sure broadcasts are disabled on 'delete' """
        contact = self.create_contact()
        group = self.create_group()
        contact.groups.add(group)
        before = self.create_broadcast(when='future',
                                       data={'groups': [group.pk]})
        url = reverse('delete-broadcast', args=[before.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302,
                         'should redirect on success')
        after = Broadcast.objects.get(pk=before.pk)
        self.assertTrue(after.schedule_frequency is None)


class BroadcastScriptedTest(TestScript, BroadcastCreateDataTest):
    def test_entire_stack(self):
        self.startRouter()
        contact = self.create_contact()
        backend = self.create_backend(data={'name': 'mockbackend'})
        connection = self.create_connection({'contact': contact,
                                             'backend': backend})
        # ready broadcast
        g1 = self.create_group()
        contact.groups.add(g1)
        b1 = self.create_broadcast(when='past', data={'groups': [g1]})
        # non-ready broadcast
        g2 = self.create_group()
        contact.groups.add(g2)
        b2 = self.create_broadcast(when='future', data={'groups': [g2]})
        # run cronjob
        scheduler_callback(self.router)
        queued = contact.broadcast_messages.filter(status='queued').count()
        sent = contact.broadcast_messages.filter(status='sent').count()
        # nothing should be queued (future broadcast isn't ready)
        self.assertEqual(queued, 0)
        # only one message should be sent
        self.assertEqual(sent, 1)
        message = contact.broadcast_messages.filter(status='sent')[0]
        self.assertTrue(message.date_sent is not None)
        self.stopRouter()

