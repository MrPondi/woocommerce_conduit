import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.password import get_decrypted_password

from woocommerce_conduit.patches.v1.encrypt_webhook_secrets import execute

PLAIN, MASKED, EMPTY = "plain.example.com", "masked.example.com", "empty.example.com"
SECRET = "s3cret-of-the-webhooks"


class TestEncryptWebhookSecrets(IntegrationTestCase):
	def setUp(self):
		frappe.db.savepoint("test_encrypt_webhook_secrets")
		self.addCleanup(frappe.db.rollback, save_point="test_encrypt_webhook_secrets")
		# As the servers were stored while the secret was a Small Text: in their column, with nothing encrypted
		for name, secret in ((PLAIN, SECRET), (MASKED, "*" * 12), (EMPTY, None)):
			frappe.get_doc(
				{
					"doctype": "WooCommerce Server",
					"name": name,
					"woocommerce_server_url": f"https://{name}",
					"secret": secret,
				}
			).db_insert()

	def stored(self, name):
		return frappe.db.get_value("WooCommerce Server", name, "secret")

	def test_a_secret_in_its_column_is_encrypted_and_masked(self):
		execute()

		self.assertEqual(self.stored(PLAIN), "*" * len(SECRET))
		self.assertEqual(get_decrypted_password("WooCommerce Server", PLAIN, "secret"), SECRET)
		# What the webhook endpoint reads
		self.assertEqual(
			frappe.get_doc("WooCommerce Server", PLAIN).get_password("secret", raise_exception=False), SECRET
		)

	def test_a_masked_or_empty_secret_is_left_alone(self):
		execute()

		self.assertEqual(self.stored(MASKED), "*" * 12)
		self.assertIsNone(self.stored(EMPTY))
		for name in (MASKED, EMPTY):
			self.assertIsNone(
				get_decrypted_password("WooCommerce Server", name, "secret", raise_exception=False)
			)

	def test_running_it_again_changes_nothing(self):
		execute()
		execute()

		self.assertEqual(self.stored(PLAIN), "*" * len(SECRET))
		self.assertEqual(get_decrypted_password("WooCommerce Server", PLAIN, "secret"), SECRET)
