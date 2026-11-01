import frappe
from frappe.utils import now
from frappe.utils.background_jobs import get_job_status
from rq.job import JobStatus


def record_sync_time(server_name: str):
	"""
	Note on the WooCommerce Server when prices or stock were last pushed to it. Written with db.set_value, so
	the server doesn't get a new modified timestamp (and a version) for every push.
	"""
	frappe.db.set_value("WooCommerce Server", server_name, "last_sync_time", now(), update_modified=False)


def enqueue_unless_queued(method: str, job_id: str, **kwargs):
	"""
	Enqueue a job, unless a job with this id is still waiting in the queue: that one reads the values when
	it runs. A job that has started may have read them already, so it doesn't count, unlike with Frappe's
	`deduplicate`. The next job then gets an id of its own, as the started job holds this one.
	"""
	status = get_job_status(job_id)
	if status == JobStatus.QUEUED:
		return
	if status == JobStatus.STARTED:
		frappe.enqueue(method, **kwargs)
	else:
		frappe.enqueue(method, job_id=job_id, deduplicate=True, **kwargs)


def enqueue_after_commit_unless_queued(method: str, job_id: str, **kwargs):
	"""
	enqueue_unless_queued once the transaction is committed. A job that is still waiting then starts after
	the commit, so it reads what this transaction wrote.
	"""
	frappe.db.after_commit.add(lambda: enqueue_unless_queued(method, job_id, **kwargs))
