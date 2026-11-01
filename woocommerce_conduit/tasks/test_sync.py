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
