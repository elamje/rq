# -*- coding: utf-8 -*-
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import sys
import time
import zlib
from datetime import datetime

from rq.compat import PY2, as_text
from rq.exceptions import NoSuchJobError, UnpickleError
from rq.job import Job, JobStatus, cancel_job, get_current_job
from rq.queue import Queue
from rq.registry import (DeferredJobRegistry, FailedJobRegistry,
                         FinishedJobRegistry, StartedJobRegistry)
from rq.utils import utcformat
from rq.worker import Worker
from tests import RQTestCase, fixtures

is_py2 = sys.version[0] == '2'
if is_py2:
    import Queue as queue
else:
    import queue as queue



try:
    from cPickle import loads, dumps
except ImportError:
    from pickle import loads, dumps


class TestJob(RQTestCase):
    def test_unicode(self):
        """Unicode in job description [issue405]"""
        job = Job.create(
            'myfunc',
            args=[12, "☃"],
            kwargs=dict(snowman="☃", null=None),
        )

        if not PY2:
            # Python 3
            expected_string = "myfunc(12, '☃', null=None, snowman='☃')"
        else:
            # Python 2
            expected_string = u"myfunc(12, u'\\u2603', null=None, snowman=u'\\u2603')".decode('utf-8')

        self.assertEqual(
            job.description,
            expected_string,
        )

    def test_create_empty_job(self):
        """Creation of new empty jobs."""
        job = Job()
        job.description = 'test job'

        # Jobs have a random UUID and a creation date
        self.assertIsNotNone(job.id)
        self.assertIsNotNone(job.created_at)
        self.assertEqual(str(job), "<Job %s: test job>" % job.id)

        # ...and nothing else
        self.assertIsNone(job.origin)
        self.assertIsNone(job.enqueued_at)
        self.assertIsNone(job.started_at)
        self.assertIsNone(job.ended_at)
        self.assertIsNone(job.result)
        self.assertIsNone(job.exc_info)

        with self.assertRaises(ValueError):
            job.func
        with self.assertRaises(ValueError):
            job.instance
        with self.assertRaises(ValueError):
            job.args
        with self.assertRaises(ValueError):
            job.kwargs

    def test_create_param_errors(self):
        """Creation of jobs may result in errors"""
        self.assertRaises(TypeError, Job.create, fixtures.say_hello, args="string")
        self.assertRaises(TypeError, Job.create, fixtures.say_hello, kwargs="string")
        self.assertRaises(TypeError, Job.create, func=42)

    def test_create_typical_job(self):
        """Creation of jobs for function calls."""
        job = Job.create(func=fixtures.some_calculation, args=(3, 4), kwargs=dict(z=2))

        # Jobs have a random UUID
        self.assertIsNotNone(job.id)
        self.assertIsNotNone(job.created_at)
        self.assertIsNotNone(job.description)
        self.assertIsNone(job.instance)

        # Job data is set...
        self.assertEqual(job.func, fixtures.some_calculation)
        self.assertEqual(job.args, (3, 4))
        self.assertEqual(job.kwargs, {'z': 2})

        # ...but metadata is not
        self.assertIsNone(job.origin)
        self.assertIsNone(job.enqueued_at)
        self.assertIsNone(job.result)

    def test_create_instance_method_job(self):
        """Creation of jobs for instance methods."""
        n = fixtures.Number(2)
        job = Job.create(func=n.div, args=(4,))

        # Job data is set
        self.assertEqual(job.func, n.div)
        self.assertEqual(job.instance, n)
        self.assertEqual(job.args, (4,))

    def test_create_job_from_string_function(self):
        """Creation of jobs using string specifier."""
        job = Job.create(func='tests.fixtures.say_hello', args=('World',))

        # Job data is set
        self.assertEqual(job.func, fixtures.say_hello)
        self.assertIsNone(job.instance)
        self.assertEqual(job.args, ('World',))

    def test_create_job_from_callable_class(self):
        """Creation of jobs using a callable class specifier."""
        kallable = fixtures.CallableObject()
        job = Job.create(func=kallable)

        self.assertEqual(job.func, kallable.__call__)
        self.assertEqual(job.instance, kallable)

    def test_job_properties_set_data_property(self):
        """Data property gets derived from the job tuple."""
        job = Job()
        job.func_name = 'foo'
        fname, instance, args, kwargs = loads(job.data)

        self.assertEqual(fname, job.func_name)
        self.assertEqual(instance, None)
        self.assertEqual(args, ())
        self.assertEqual(kwargs, {})

    def test_data_property_sets_job_properties(self):
        """Job tuple gets derived lazily from data property."""
        job = Job()
        job.data = dumps(('foo', None, (1, 2, 3), {'bar': 'qux'}))

        self.assertEqual(job.func_name, 'foo')
        self.assertEqual(job.instance, None)
        self.assertEqual(job.args, (1, 2, 3))
        self.assertEqual(job.kwargs, {'bar': 'qux'})

    def test_save(self):  # noqa
        """Storing jobs."""
        job = Job.create(func=fixtures.some_calculation, args=(3, 4), kwargs=dict(z=2))

        # Saving creates a Redis hash
        self.assertEqual(self.testconn.exists(job.key), False)
        job.save()
        self.assertEqual(self.testconn.type(job.key), b'hash')

        # Saving writes pickled job data
        unpickled_data = loads(zlib.decompress(self.testconn.hget(job.key, 'data')))
        self.assertEqual(unpickled_data[0], 'tests.fixtures.some_calculation')

    def test_fetch(self):
        """Fetching jobs."""
        # Prepare test
        self.testconn.hset('rq:job:some_id', 'data',
                           "(S'tests.fixtures.some_calculation'\nN(I3\nI4\nt(dp1\nS'z'\nI2\nstp2\n.")
        self.testconn.hset('rq:job:some_id', 'created_at',
                           '2012-02-07T22:13:24.123456Z')

        # Fetch returns a job
        job = Job.fetch('some_id')
        self.assertEqual(job.id, 'some_id')
        self.assertEqual(job.func_name, 'tests.fixtures.some_calculation')
        self.assertIsNone(job.instance)
        self.assertEqual(job.args, (3, 4))
        self.assertEqual(job.kwargs, dict(z=2))
        self.assertEqual(job.created_at, datetime(2012, 2, 7, 22, 13, 24, 123456))

    def test_fetch_many(self):
        """Fetching many jobs at once."""
        data = {
            'func': fixtures.some_calculation,
            'args': (3, 4),
            'kwargs': dict(z=2),
            'connection': self.testconn,
        }
        job = Job.create(**data)
        job.save()

        job2 = Job.create(**data)
        job2.save()

        jobs = Job.fetch_many([job.id, job2.id, 'invalid_id'], self.testconn)
        self.assertEqual(jobs, [job, job2, None])

    def test_persistence_of_empty_jobs(self):  # noqa
        """Storing empty jobs."""
        job = Job()
        with self.assertRaises(ValueError):
            job.save()

    def test_persistence_of_typical_jobs(self):
        """Storing typical jobs."""
        job = Job.create(func=fixtures.some_calculation, args=(3, 4), kwargs=dict(z=2))
        job.save()

        stored_date = self.testconn.hget(job.key, 'created_at').decode('utf-8')
        self.assertEqual(stored_date, utcformat(job.created_at))

        # ... and no other keys are stored
        self.assertEqual(
            sorted(self.testconn.hkeys(job.key)),
            [b'created_at', b'data', b'description'])

    def test_persistence_of_parent_job(self):
        """Storing jobs with parent job, either instance or key."""
        parent_job = Job.create(func=fixtures.some_calculation)
        parent_job.save()
        job = Job.create(func=fixtures.some_calculation, depends_on=parent_job)
        job.save()
        stored_job = Job.fetch(job.id)
        self.assertEqual(stored_job._dependency_id, parent_job.id)
        self.assertEqual(stored_job._dependency_ids, [parent_job.id])
        self.assertEqual(stored_job.dependency.id, parent_job.id)
        self.assertEqual(stored_job.dependency, parent_job)

        job = Job.create(func=fixtures.some_calculation, depends_on=parent_job.id)
        job.save()
        stored_job = Job.fetch(job.id)
        self.assertEqual(stored_job._dependency_id, parent_job.id)
        self.assertEqual(stored_job._dependency_ids, [parent_job.id])
        self.assertEqual(stored_job.dependency.id, parent_job.id)
        self.assertEqual(stored_job.dependency, parent_job)

    def test_store_then_fetch(self):
        """Store, then fetch."""
        job = Job.create(func=fixtures.some_calculation, timeout='1h', args=(3, 4), kwargs=dict(z=2))
        job.save()

        job2 = Job.fetch(job.id)
        self.assertEqual(job.func, job2.func)
        self.assertEqual(job.args, job2.args)
        self.assertEqual(job.kwargs, job2.kwargs)
        self.assertEqual(job.timeout, job2.timeout)

        # Mathematical equation
        self.assertEqual(job, job2)

    def test_fetching_can_fail(self):
        """Fetching fails for non-existing jobs."""
        with self.assertRaises(NoSuchJobError):
            Job.fetch('b4a44d44-da16-4620-90a6-798e8cd72ca0')

    def test_fetching_unreadable_data(self):
        """Fetching succeeds on unreadable data, but lazy props fail."""
        # Set up
        job = Job.create(func=fixtures.some_calculation, args=(3, 4),
                         kwargs=dict(z=2))
        job.save()

        # Just replace the data hkey with some random noise
        self.testconn.hset(job.key, 'data', 'this is no pickle string')
        job.refresh()

        for attr in ('func_name', 'instance', 'args', 'kwargs'):
            with self.assertRaises(UnpickleError):
                getattr(job, attr)

    def test_job_is_unimportable(self):
        """Jobs that cannot be imported throw exception on access."""
        job = Job.create(func=fixtures.say_hello, args=('Lionel',))
        job.save()

        # Now slightly modify the job to make it unimportable (this is
        # equivalent to a worker not having the most up-to-date source code
        # and unable to import the function)
        job_data = job.data
        unimportable_data = job_data.replace(b'say_hello', b'nay_hello')

        self.testconn.hset(job.key, 'data', zlib.compress(unimportable_data))

        job.refresh()
        with self.assertRaises(AttributeError):
            job.func  # accessing the func property should fail

    def test_compressed_exc_info_handling(self):
        """Jobs handle both compressed and uncompressed exc_info"""
        exception_string = 'Some exception'

        job = Job.create(func=fixtures.say_hello, args=('Lionel',))
        job.exc_info = exception_string
        job.save()

        # exc_info is stored in compressed format
        exc_info = self.testconn.hget(job.key, 'exc_info')
        self.assertEqual(
            as_text(zlib.decompress(exc_info)),
            exception_string
        )

        job.refresh()
        self.assertEqual(job.exc_info, exception_string)

        # Uncompressed exc_info is also handled
        self.testconn.hset(job.key, 'exc_info', exception_string)

        job.refresh()
        self.assertEqual(job.exc_info, exception_string)

    def test_compressed_job_data_handling(self):
        """Jobs handle both compressed and uncompressed data"""

        job = Job.create(func=fixtures.say_hello, args=('Lionel',))
        job.save()

        # Job data is stored in compressed format
        job_data = job.data
        self.assertEqual(
            zlib.compress(job_data),
            self.testconn.hget(job.key, 'data')
        )

        self.testconn.hset(job.key, 'data', job_data)
        job.refresh()
        self.assertEqual(job.data, job_data)


    def test_custom_meta_is_persisted(self):
        """Additional meta data on jobs are stored persisted correctly."""
        job = Job.create(func=fixtures.say_hello, args=('Lionel',))
        job.meta['foo'] = 'bar'
        job.save()

        raw_data = self.testconn.hget(job.key, 'meta')
        self.assertEqual(loads(raw_data)['foo'], 'bar')

        job2 = Job.fetch(job.id)
        self.assertEqual(job2.meta['foo'], 'bar')

    def test_custom_meta_is_rewriten_by_save_meta(self):
        """New meta data can be stored by save_meta."""
        job = Job.create(func=fixtures.say_hello, args=('Lionel',))
        job.save()
        serialized = job.to_dict()

        job.meta['foo'] = 'bar'
        job.save_meta()

        raw_meta = self.testconn.hget(job.key, 'meta')
        self.assertEqual(loads(raw_meta)['foo'], 'bar')

        job2 = Job.fetch(job.id)
        self.assertEqual(job2.meta['foo'], 'bar')

        # nothing else was changed
        serialized2 = job2.to_dict()
        serialized2.pop('meta')
        self.assertDictEqual(serialized, serialized2)

    def test_unpickleable_result(self):
        """Unpickleable job result doesn't crash job.to_dict()"""
        job = Job.create(func=fixtures.say_hello, args=('Lionel',))
        job._result = queue.Queue()
        data = job.to_dict()
        self.assertEqual(data['result'], 'Unpickleable return value')

    def test_result_ttl_is_persisted(self):
        """Ensure that job's result_ttl is set properly"""
        job = Job.create(func=fixtures.say_hello, args=('Lionel',), result_ttl=10)
        job.save()
        Job.fetch(job.id, connection=self.testconn)
        self.assertEqual(job.result_ttl, 10)

        job = Job.create(func=fixtures.say_hello, args=('Lionel',))
        job.save()
        Job.fetch(job.id, connection=self.testconn)
        self.assertEqual(job.result_ttl, None)

    def test_failure_ttl_is_persisted(self):
        """Ensure job.failure_ttl is set and restored properly"""
        job = Job.create(func=fixtures.say_hello, args=('Lionel',), failure_ttl=15)
        job.save()
        Job.fetch(job.id, connection=self.testconn)
        self.assertEqual(job.failure_ttl, 15)

        job = Job.create(func=fixtures.say_hello, args=('Lionel',))
        job.save()
        Job.fetch(job.id, connection=self.testconn)
        self.assertEqual(job.failure_ttl, None)

    def test_description_is_persisted(self):
        """Ensure that job's custom description is set properly"""
        job = Job.create(func=fixtures.say_hello, args=('Lionel',), description='Say hello!')
        job.save()
        Job.fetch(job.id, connection=self.testconn)
        self.assertEqual(job.description, 'Say hello!')

        # Ensure job description is constructed from function call string
        job = Job.create(func=fixtures.say_hello, args=('Lionel',))
        job.save()
        Job.fetch(job.id, connection=self.testconn)
        if PY2:
            self.assertEqual(job.description, "tests.fixtures.say_hello(u'Lionel')")
        else:
            self.assertEqual(job.description, "tests.fixtures.say_hello('Lionel')")

    def test_job_access_outside_job_fails(self):
        """The current job is accessible only within a job context."""
        self.assertIsNone(get_current_job())

    def test_job_access_within_job_function(self):
        """The current job is accessible within the job function."""
        q = Queue()
        job = q.enqueue(fixtures.access_self)
        w = Worker([q])
        w.work(burst=True)
        # access_self calls get_current_job() and executes successfully
        self.assertEqual(job.get_status(), JobStatus.FINISHED)

    def test_job_access_within_synchronous_job_function(self):
        queue = Queue(is_async=False)
        queue.enqueue(fixtures.access_self)

    def test_job_async_status_finished(self):
        queue = Queue(is_async=False)
        job = queue.enqueue(fixtures.say_hello)
        self.assertEqual(job.result, 'Hi there, Stranger!')
        self.assertEqual(job.get_status(), JobStatus.FINISHED)

    def test_enqueue_job_async_status_finished(self):
        queue = Queue(is_async=False)
        job = Job.create(func=fixtures.say_hello)
        job = queue.enqueue_job(job)
        self.assertEqual(job.result, 'Hi there, Stranger!')
        self.assertEqual(job.get_status(), JobStatus.FINISHED)

    def test_get_result_ttl(self):
        """Getting job result TTL."""
        job_result_ttl = 1
        default_ttl = 2
        job = Job.create(func=fixtures.say_hello, result_ttl=job_result_ttl)
        job.save()
        self.assertEqual(job.get_result_ttl(default_ttl=default_ttl), job_result_ttl)
        self.assertEqual(job.get_result_ttl(), job_result_ttl)
        job = Job.create(func=fixtures.say_hello)
        job.save()
        self.assertEqual(job.get_result_ttl(default_ttl=default_ttl), default_ttl)
        self.assertEqual(job.get_result_ttl(), None)

    def test_get_job_ttl(self):
        """Getting job TTL."""
        ttl = 1
        job = Job.create(func=fixtures.say_hello, ttl=ttl)
        job.save()
        self.assertEqual(job.get_ttl(), ttl)
        job = Job.create(func=fixtures.say_hello)
        job.save()
        self.assertEqual(job.get_ttl(), None)

    def test_ttl_via_enqueue(self):
        ttl = 1
        queue = Queue(connection=self.testconn)
        job = queue.enqueue(fixtures.say_hello, ttl=ttl)
        self.assertEqual(job.get_ttl(), ttl)

    def test_never_expire_during_execution(self):
        """Test what happens when job expires during execution"""
        ttl = 1
        queue = Queue(connection=self.testconn)
        job = queue.enqueue(fixtures.long_running_job, args=(2,), ttl=ttl)
        self.assertEqual(job.get_ttl(), ttl)
        job.save()
        job.perform()
        self.assertEqual(job.get_ttl(), ttl)
        self.assertTrue(job.exists(job.id))
        self.assertEqual(job.result, 'Done sleeping...')

    def test_cleanup(self):
        """Test that jobs and results are expired properly."""
        job = Job.create(func=fixtures.say_hello)
        job.save()

        # Jobs with negative TTLs don't expire
        job.cleanup(ttl=-1)
        self.assertEqual(self.testconn.ttl(job.key), -1)

        # Jobs with positive TTLs are eventually deleted
        job.cleanup(ttl=100)
        self.assertEqual(self.testconn.ttl(job.key), 100)

        # Jobs with 0 TTL are immediately deleted
        job.cleanup(ttl=0)
        self.assertRaises(NoSuchJobError, Job.fetch, job.id, self.testconn)

    def test_job_with_dependents_delete_parent(self):
        """job.delete() deletes itself from Redis but not dependents.
        Wthout a save, the dependent job is never saved into redis. The delete
        method will get and pass a NoSuchJobError.
        """
        queue = Queue(connection=self.testconn)
        job = queue.enqueue(fixtures.say_hello)
        job2 = Job.create(func=fixtures.say_hello, depends_on=job)
        job2.register_dependency()

        job.delete()
        self.assertFalse(self.testconn.exists(job.key))
        self.assertFalse(self.testconn.exists(job.dependents_key))

        # By default, dependents are not deleted, but The job is in redis only
        # if it was saved!
        self.assertFalse(self.testconn.exists(job2.key))

        self.assertNotIn(job.id, queue.get_job_ids())

    def test_job_delete_removes_itself_from_registries(self):
        """job.delete() should remove itself from job registries"""
        connection = self.testconn
        job = Job.create(func=fixtures.say_hello, status=JobStatus.FAILED,
                         connection=self.testconn, origin='default')
        job.save()
        registry = FailedJobRegistry(connection=self.testconn)
        registry.add(job, 500)

        job.delete()
        self.assertFalse(job in registry)

        job = Job.create(func=fixtures.say_hello, status=JobStatus.FINISHED,
                         connection=self.testconn, origin='default')
        job.save()

        registry = FinishedJobRegistry(connection=self.testconn)
        registry.add(job, 500)

        job.delete()
        self.assertFalse(job in registry)

        job = Job.create(func=fixtures.say_hello, status=JobStatus.STARTED,
                         connection=self.testconn, origin='default')
        job.save()

        registry = StartedJobRegistry(connection=self.testconn)
        registry.add(job, 500)

        job.delete()
        self.assertFalse(job in registry)

        job = Job.create(func=fixtures.say_hello, status=JobStatus.DEFERRED,
                         connection=self.testconn, origin='default')
        job.save()

        registry = DeferredJobRegistry(connection=self.testconn)
        registry.add(job, 500)

        job.delete()
        self.assertFalse(job in registry)

    def test_job_with_dependents_delete_parent_with_saved(self):
        """job.delete() deletes itself from Redis but not dependents. If the
        dependent job was saved, it will remain in redis."""
        queue = Queue(connection=self.testconn)
        job = queue.enqueue(fixtures.say_hello)
        job2 = Job.create(func=fixtures.say_hello, depends_on=job)
        job2.register_dependency()
        job2.save()

        job.delete()
        self.assertFalse(self.testconn.exists(job.key))
        self.assertFalse(self.testconn.exists(job.dependents_key))

        # By default, dependents are not deleted, but The job is in redis only
        # if it was saved!
        self.assertTrue(self.testconn.exists(job2.key))

        self.assertNotIn(job.id, queue.get_job_ids())

    def test_job_with_dependents_deleteall(self):
        """job.delete() deletes itself from Redis. Dependents need to be
        deleted explictely."""
        queue = Queue(connection=self.testconn)
        job = queue.enqueue(fixtures.say_hello)
        job2 = Job.create(func=fixtures.say_hello, depends_on=job)
        job2.register_dependency()

        job.delete(delete_dependents=True)
        self.assertFalse(self.testconn.exists(job.key))
        self.assertFalse(self.testconn.exists(job.dependents_key))
        self.assertFalse(self.testconn.exists(job2.key))

        self.assertNotIn(job.id, queue.get_job_ids())

    def test_job_with_dependents_delete_all_with_saved(self):
        """job.delete() deletes itself from Redis. Dependents need to be
        deleted explictely. Without a save, the dependent job is never saved
        into redis. The delete method will get and pass a NoSuchJobError.
        """
        queue = Queue(connection=self.testconn)
        job = queue.enqueue(fixtures.say_hello)
        job2 = Job.create(func=fixtures.say_hello, depends_on=job)
        job2.register_dependency()
        job2.save()

        job.delete(delete_dependents=True)
        self.assertFalse(self.testconn.exists(job.key))
        self.assertFalse(self.testconn.exists(job.dependents_key))
        self.assertFalse(self.testconn.exists(job2.key))

        self.assertNotIn(job.id, queue.get_job_ids())

    def test_create_job_with_id(self):
        """test creating jobs with a custom ID"""
        queue = Queue(connection=self.testconn)
        job = queue.enqueue(fixtures.say_hello, job_id="1234")
        self.assertEqual(job.id, "1234")
        job.perform()

        self.assertRaises(TypeError, queue.enqueue, fixtures.say_hello, job_id=1234)

    def test_get_call_string_unicode(self):
        """test call string with unicode keyword arguments"""
        queue = Queue(connection=self.testconn)

        job = queue.enqueue(fixtures.echo, arg_with_unicode=fixtures.UnicodeStringObject())
        self.assertIsNotNone(job.get_call_string())
        job.perform()

    def test_create_job_with_ttl_should_have_ttl_after_enqueued(self):
        """test creating jobs with ttl and checks if get_jobs returns it properly [issue502]"""
        queue = Queue(connection=self.testconn)
        queue.enqueue(fixtures.say_hello, job_id="1234", ttl=10)
        job = queue.get_jobs()[0]
        self.assertEqual(job.ttl, 10)

    def test_create_job_with_ttl_should_expire(self):
        """test if a job created with ttl expires [issue502]"""
        queue = Queue(connection=self.testconn)
        queue.enqueue(fixtures.say_hello, job_id="1234", ttl=1)
        time.sleep(1.1)
        self.assertEqual(0, len(queue.get_jobs()))

    def test_create_and_cancel_job(self):
        """test creating and using cancel_job deletes job properly"""
        queue = Queue(connection=self.testconn)
        job = queue.enqueue(fixtures.say_hello)
        self.assertEqual(1, len(queue.get_jobs()))
        cancel_job(job.id)
        self.assertEqual(0, len(queue.get_jobs()))

    def test_dependents_key_for_should_return_prefixed_job_id(self):
        """test redis key to store job dependents hash under"""
        job_id = 'random'
        key = Job.dependents_key_for(job_id=job_id)

        assert key == Job.redis_job_namespace_prefix + job_id + ':dependents'

    def test_key_for_should_return_prefixed_job_id(self):
        """test redis key to store job hash under"""
        job_id = 'random'
        key = Job.key_for(job_id=job_id)

        assert key == (Job.redis_job_namespace_prefix + job_id).encode('utf-8')
