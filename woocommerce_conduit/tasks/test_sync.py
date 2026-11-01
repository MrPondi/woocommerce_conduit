from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from rq.job import JobStatus

from woocommerce_conduit.tasks import sync
from woocommerce_conduit.tasks.sync import enqueue_after_commit_unless_queued, enqueue_unless_queued


class TestEnqueueUnlessQueued(IntegrationTestCase):
	def enqueue(self, status):
		with (
			patch.object(sync, "get_job_status", return_value=status),
			patch("frappe.enqueue") as enqueue,
		):
			enqueue_unless_queued("app.method", job_id="push::MUG-1", item_code="MUG-1")
		return enqueue

	def test_a_waiting_job_takes_the_change_along(self):
		self.enqueue(JobStatus.QUEUED).assert_not_called()

	def test_a_started_job_may_have_read_the_old_values(self):
		self.enqueue(JobStatus.STARTED).assert_called_once_with("app.method", item_code="MUG-1")

	def test_otherwise_the_job_goes_by_its_id(self):
		self.enqueue(None).assert_called_once_with(
			"app.method", job_id="push::MUG-1", deduplicate=True, item_code="MUG-1"
		)

	def test_the_check_waits_for_the_commit(self):
		with (
			patch.object(frappe.db, "after_commit") as after_commit,
			patch.object(sync, "enqueue_unless_queued") as enqueue,
		):
			enqueue_after_commit_unless_queued("app.method", job_id="push::MUG-1", item_code="MUG-1")
			enqueue.assert_not_called()
			after_commit.add.call_args.args[0]()

		enqueue.assert_called_once_with("app.method", "push::MUG-1", item_code="MUG-1")


class TestUnescapeText(IntegrationTestCase):
	def test_escaped_text_reads_plain(self):
		self.assertEqual(sync.unescape_text("Sleeves &amp; Toploader"), "Sleeves & Toploader")
		self.assertEqual(sync.unescape_text("Pan&#8217;s &quot;best&quot;"), 'Pan\u2019s "best"')
		# Escaped twice, it is unescaped once
		self.assertEqual(sync.unescape_text("Tom &amp;amp; Jerry"), "Tom &amp; Jerry")

	def test_plain_text_is_left_alone(self):
		# html.unescape would read the &reg of Smith&regular, and the &copy without its semicolon
		for text in ("Smith&regular", "R&copy 2024", "AT&T", "Mug"):
			with self.subTest(text=text):
				self.assertEqual(sync.unescape_text(text), text)

	def test_anything_but_text_is_left_alone(self):
		for value in (None, 12, ["&amp;"], {"name": "&amp;"}):
			with self.subTest(value=value):
				self.assertEqual(sync.unescape_text(value), value)
