# Copyright © 2019 Province of British Columbia
#
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Service to manage PayBC interaction."""

from datetime import datetime
from typing import Any, Dict

from flask import current_app
from requests.exceptions import HTTPError

from pay_api.exceptions import BusinessException, Error
from pay_api.models.corp_type import CorpType
from pay_api.utils.enums import AuthHeaderType, ContentType, InvoiceStatus, PaymentStatus
from pay_api.utils.enums import PaymentSystem as PaySystemCode, PaymentMethod
from pay_api.utils.errors import get_bcol_error
from pay_api.utils.user_context import UserContext
from pay_api.utils.user_context import user_context
from pay_api.utils.util import generate_transaction_number
from .base_payment_system import PaymentSystemService
from .invoice import Invoice
from .invoice_reference import InvoiceReference
from .oauth_service import OAuthService
from .payment_account import PaymentAccount
from .payment_line_item import PaymentLineItem


class BcolService(PaymentSystemService, OAuthService):
    """Service to manage BCOL integration."""

    def create_account(self, name: str, contact_info: Dict[str, Any], payment_info: Dict[str, Any],
                       **kwargs) -> any:
        """Return an empty value since we don't create BC Online account."""

    def update_account(self, name: str, cfs_account: any, payment_info: Dict[str, Any]) -> any:
        """No BCOL account update."""

    def get_payment_system_code(self):
        """Return PAYBC as the system code."""
        return PaySystemCode.BCOL.value

    def get_default_invoice_status(self) -> str:
        """Return CREATED as the default invoice status."""
        return InvoiceStatus.CREATED.value

    def get_default_payment_status(self) -> str:
        """Return the default status for payment when created."""
        return PaymentStatus.CREATED.value

    @user_context
    def create_invoice(self, payment_account: PaymentAccount,  # pylint: disable=too-many-locals
                       line_items: [PaymentLineItem], invoice: Invoice, **kwargs) -> InvoiceReference:
        """Create Invoice in PayBC."""
        current_app.logger.debug('<create_invoice')
        user: UserContext = kwargs['user']
        pay_endpoint = current_app.config.get('BCOL_API_ENDPOINT') + '/payments'
        corp_number = invoice.business_identifier
        amount_excluding_txn_fees = sum(line.total for line in line_items)
        filing_types = ','.join([item.filing_type_code for item in line_items])
        remarks = f'{corp_number}({filing_types})'
        if user.first_name:
            remarks = f'{remarks}-{user.first_name}'

        payload: Dict = {
            # 'userId': payment_account.bcol_user_id if payment_account.bcol_user_id else 'PE25020',
            'userId': payment_account.bcol_user_id,
            'invoiceNumber': generate_transaction_number(invoice.id),
            'folioNumber': invoice.folio_number,
            'amount': str(amount_excluding_txn_fees),
            'rate': str(amount_excluding_txn_fees),
            'remarks': remarks[:50],
            'feeCode': self._get_fee_code(invoice.corp_type_code, user.is_staff() or user.is_system())
        }

        if user.is_staff() or user.is_system():
            payload['userId'] = user.user_name_with_no_idp if user.is_staff() else current_app.config[
                'BCOL_USERNAME_FOR_SERVICE_ACCOUNT_PAYMENTS']
            payload['accountNumber'] = invoice.bcol_account
            payload['formNumber'] = invoice.dat_number
            payload['reduntantFlag'] = 'Y'
            payload['rateType'] = 'C'

        if payload.get('folioNumber', None) is None:  # Set empty folio if None
            payload['folioNumber'] = ''
        try:
            pay_response = self.post(pay_endpoint, user.bearer_token, AuthHeaderType.BEARER, ContentType.JSON,
                                     payload, raise_for_error=False)
            response_json = pay_response.json()
            current_app.logger.debug(response_json)
            pay_response.raise_for_status()
        except HTTPError as bol_err:
            current_app.logger.error(bol_err)
            error_type: str = response_json.get('type')
            if error_type.isdigit():
                error = get_bcol_error(int(error_type))
            else:
                error = Error.BCOL_ERROR
            raise BusinessException(error)

        invoice_reference: InvoiceReference = InvoiceReference.create(invoice.id, response_json.get('key'),
                                                                      response_json.get('sequenceNo'))

        current_app.logger.debug('>create_invoice')
        return invoice_reference

    def update_invoice(self, payment_account: PaymentAccount,  # pylint:disable=too-many-arguments
                       line_items: [PaymentLineItem], invoice_id: int, paybc_inv_number: str, reference_count: int = 0,
                       **kwargs):
        """Adjust the invoice."""
        current_app.logger.debug('<update_invoice')

    def cancel_invoice(self, payment_account: PaymentAccount, inv_number: str):
        """Adjust the invoice to zero."""
        current_app.logger.debug('<cancel_invoice')

    def get_receipt(self, payment_account: PaymentAccount, pay_response_url: str, invoice_reference: InvoiceReference):
        """Get receipt from bcol for the receipt number or get receipt against invoice number."""
        current_app.logger.debug('<get_receipt')
        invoice = Invoice.find_by_id(invoice_reference.invoice_id, skip_auth_check=True)
        return f'{invoice_reference.invoice_number}', datetime.now(), invoice.total

    def _get_fee_code(self, corp_type: str, is_staff: bool = False):  # pylint: disable=no-self-use
        """Return BCOL fee code."""
        corp_type = CorpType.find_by_code(code=corp_type)
        return corp_type.bcol_staff_fee_code if is_staff else corp_type.bcol_fee_code

    def get_payment_method_code(self):
        """Return CC as the method code."""
        return PaymentMethod.DRAWDOWN.value

    def complete_post_invoice(self, invoice: Invoice, invoice_reference: InvoiceReference) -> None:
        """Complete any post payment activities if needed."""
        # pylint: disable=cyclic-import,import-outside-toplevel
        from .payment_transaction import PaymentTransaction
        from .payment import Payment

        # Create a payment record
        Payment.create(payment_method=self.get_payment_method_code(),
                       payment_system=self.get_payment_system_code(),
                       payment_status=self.get_default_payment_status(),
                       invoice_number=invoice_reference.invoice_number,
                       invoice_amount=invoice.total,
                       payment_account_id=invoice.payment_account_id)
        transaction: PaymentTransaction = PaymentTransaction.create_transaction_for_invoice(
            invoice.id,
            {
                'clientSystemUrl': '',
                'payReturnUrl': ''
            }
        )
        transaction.update_transaction(transaction.id, pay_response_url=None)
