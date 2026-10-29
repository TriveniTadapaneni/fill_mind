import io
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from scripts.monitor_run import GitHub, MonitorError, UncertainRerun, monitor, rate_limit_delay


def run(attempt=1, conclusion=None, status='completed'):
    return dict(run_attempt=attempt, conclusion=conclusion, status=status)


class FakeAPI:
    def __init__(self, states, uncertain=False):
        self.states = iter(states)
        self.posts = 0
        self.pauses = []
        self.uncertain = uncertain

    def request(self, method='GET'):
        if method == 'POST':
            self.posts += 1
            if self.uncertain:
                raise UncertainRerun('response lost')
            return {}
        return next(self.states)

    def pause(self, seconds):
        self.pauses.append(seconds)


class MonitorTests(unittest.TestCase):
    def test_initial_success(self):
        api = FakeAPI([run(conclusion='success')])
        self.assertEqual(monitor(api, 2, 60), 0)
        self.assertEqual(api.posts, 0)

    def test_retry_success_waits_for_new_attempt(self):
        api = FakeAPI([run(conclusion='failure'), run(conclusion='failure'),
                       run(2, status='queued'), run(2, 'success')])
        self.assertEqual(monitor(api, 2, 60), 0)
        self.assertEqual(api.posts, 1)
        self.assertEqual(api.pauses, [10, 60])

    def test_exhausted(self):
        api = FakeAPI([run(1, 'failure'), run(2, 'timed_out'), run(3, 'failure')])
        self.assertEqual(monitor(api, 2, 60), 1)
        self.assertEqual(api.posts, 2)

    def test_zero_retries(self):
        api = FakeAPI([run(conclusion='failure')])
        self.assertEqual(monitor(api, 0, 60), 1)
        self.assertEqual(api.posts, 0)

    def test_cancelled(self):
        api = FakeAPI([run(conclusion='cancelled')])
        with self.assertRaises(MonitorError):
            monitor(api, 2, 60)
        self.assertEqual(api.posts, 0)

    def test_uncertain_post_not_repeated(self):
        api = FakeAPI([run(1, 'failure'), run(1, 'failure'), run(2, 'success')], uncertain=True)
        self.assertEqual(monitor(api, 2, 60), 0)
        self.assertEqual(api.posts, 1)

    def test_external_rerun(self):
        api = FakeAPI([run(1, status='in_progress'), run(2, 'success')])
        with self.assertRaises(MonitorError):
            monitor(api, 2, 60)

    def test_registration_timeout(self):
        api = FakeAPI([run(1, 'failure'), run(1, 'failure')])
        with patch('scripts.monitor_run.time.monotonic', side_effect=[0, 301]):
            with self.assertRaisesRegex(MonitorError, 'did not register'):
                monitor(api, 2, 60)
        self.assertEqual(api.posts, 1)

    def test_deadline(self):
        api = GitHub('owner/repo', '123', 'test-token', time.monotonic() - 1)
        with self.assertRaisesRegex(MonitorError, 'deadline'):
            api.request()

    def test_rate_limit_headers(self):
        self.assertEqual(rate_limit_delay({'Retry-After': '120'}, ''), 120)
        with patch('scripts.monitor_run.time.time', return_value=100):
            self.assertEqual(rate_limit_delay({'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '200'}, ''), 105)
        self.assertIsNone(rate_limit_delay({}, 'Resource not accessible by integration'))

    def test_permission_error_is_not_retried(self):
        api = GitHub('owner/repo', '123', 'test-token', time.monotonic() + 1000)
        error = HTTPError(api.url, 403, 'Forbidden', {}, io.BytesIO(b'access denied'))
        with patch('scripts.monitor_run.urlopen', side_effect=error) as request:
            with self.assertRaisesRegex(MonitorError, 'HTTP 403'):
                api.request()
            self.assertEqual(request.call_count, 1)

    def test_rate_limited_read_recovers(self):
        api = GitHub('owner/repo', '123', 'test-token', time.monotonic() + 1000)
        error = HTTPError(api.url, 403, 'Forbidden', {'Retry-After': '60'}, io.BytesIO(b'rate limit'))
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"status":"completed"}'
        with patch('scripts.monitor_run.urlopen', side_effect=[error, response]):
            with patch.object(api, 'pause') as pause:
                self.assertEqual(api.request()['status'], 'completed')
                pause.assert_called_once_with(60)

    def test_network_error_on_post_is_uncertain(self):
        api = GitHub('owner/repo', '123', 'test-token', time.monotonic() + 1000)
        with patch('scripts.monitor_run.urlopen', side_effect=URLError('lost')) as request:
            with self.assertRaises(UncertainRerun):
                api.request('POST')
            self.assertEqual(request.call_count, 1)


if __name__ == '__main__':
    unittest.main()
