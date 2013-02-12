# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from decimal import Decimal
import math

from endicia import CalculatingPostageAPI
from endicia.tools import objectify_response
from trytond.model import ModelView, fields
from trytond.pool import PoolMeta, Pool
from trytond.transaction import Transaction
from trytond.pyson import Eval

__all__ = ['Configuration', 'Sale', 'SaleLine']
__metaclass__ = PoolMeta


ENDICIA_PACKAGE_TYPES = [
    ('Documents', 'Documents'),
    ('Gift', 'Gift'),
    ('Merchandise', 'Merchandise'),
    ('Other', 'Other'),
    ('Sample', 'Sample')
]


class Configuration:
    'Sale Configuration'
    __name__ = 'sale.configuration'

    endicia_mailclass = fields.Many2One(
        'endicia.mailclass', 'Default MailClass',
    )
    endicia_label_subtype = fields.Selection([
        ('None', 'None'),
        ('Integrated', 'Integrated')
    ], 'Label Subtype')
    endicia_integrated_form_type = fields.Selection([
        ('Form2976', 'Form2976(Same as CN22)'),
        ('Form2976A', 'Form2976(Same as CP72)'),
    ], 'Integrated Form Type')
    endicia_include_postage = fields.Boolean('Include Postage ?')
    endicia_package_type = fields.Selection(
        ENDICIA_PACKAGE_TYPES, 'Package Content Type'
    )

    @staticmethod
    def default_endicia_label_subtype():
        # This is the default value as specified in Endicia doc
        return 'None'

    @staticmethod
    def default_endicia_integrated_form_type():
        # This is the default value as specified in Endicia doc
        return 'Form2976'

    @staticmethod
    def default_endicia_package_type():
        # This is the default value as specified in Endicia doc
        return 'Other'


class Sale:
    "Sale"
    __name__ = 'sale.sale'

    endicia_mailclass = fields.Many2One(
        'endicia.mailclass', 'MailClass', states={
            'readonly': ~Eval('state').in_(['draft', 'quotation']),
        }, depends=['state']
    )
    is_endicia_shipping = fields.Boolean(
        'Is Endicia Shipping', states={
            'readonly': ~Eval('state').in_(['draft', 'quotation']),
        }, depends=['state', 'carrier']
    )

    @staticmethod
    def default_endicia_mailclass():
        Config = Pool().get('sale.configuration')
        config = Config(1)
        return config.endicia_mailclass and config.endicia_mailclass.id or None

    @classmethod
    def __setup__(cls):
        super(Sale, cls).__setup__()
        cls._error_messages.update({
            'mailclass_missing': 'Select a mailclass to ship using Endicia ' \
                '[USPS].'
        })
        cls._buttons.update({
            'update_endicia_shipment_cost': {
                'invisible': Eval('state') != 'quotation'
            }
        })

    def on_change_carrier(self):
        res = super(Sale, self).on_change_carrier()

        res['is_endicia_shipping'] = self.carrier and \
            self.carrier.carrier_cost_method == 'endicia'

        return res

    def _get_carrier_context(self):
        "Pass sale in the context"
        context = super(Sale, self)._get_carrier_context()

        if not self.carrier.carrier_cost_method == 'endicia':
            return context

        context = context.copy()
        context['sale'] = self.id
        return context

    def on_change_lines(self):
        """Pass a flag in context which indicates the get_sale_price method
        of endicia carrier not to calculate cost on each line change
        """
        with Transaction().set_context({'ignore_carrier_computation': True}):
            return super(Sale, self).on_change_lines()

    def apply_endicia_shipping(self):
        "Add a shipping line to sale for endicia"
        Sale = Pool().get('sale.sale')

        if self.carrier and self.carrier.carrier_cost_method == 'endicia':
            if not self.endicia_mailclass:
                self.raise_user_error('mailclass_missing')
            with Transaction().set_context(self._get_carrier_context()):
                shipment_cost = self.carrier.get_sale_price()
                if not shipment_cost[0]:
                    return
            Sale.write([self], {
                'lines': [
                    ('create', {
                        'type': 'line',
                        'product': self.carrier.carrier_product.id,
                        'description': self.endicia_mailclass.name,
                        'quantity': 1,  # XXX
                        'unit': self.carrier.carrier_product.sale_uom.id,
                        'unit_price': Decimal(shipment_cost[0]),
                        'shipment_cost': Decimal(shipment_cost[0]),
                        'amount': Decimal(shipment_cost[0]),
                        'taxes': [],
                        'sequence': 9999,  # XXX
                    }),
                    ('delete', map(
                        int, [line for line in self.lines if line.shipment_cost]
                    ),
                )]
            })

    @classmethod
    def quote(cls, sales):
        res = super(Sale, cls).quote(sales)
        cls.update_endicia_shipment_cost(sales)
        return res

    @classmethod
    @ModelView.button
    def update_endicia_shipment_cost(cls, sales):
        "Updates the shipping line with new value if any"
        for sale in sales:
            sale.apply_endicia_shipping()

    def create_shipment(self, shipment_type):
        Shipment = Pool().get('stock.shipment.out')
        shipments = super(Sale, self).create_shipment(shipment_type)
        if shipment_type == 'out' and shipments and self.carrier and \
                self.carrier.carrier_cost_method == 'endicia':
            Shipment.write(shipments, {
                'endicia_mailclass': self.endicia_mailclass.id,
                'is_endicia_shipping': self.is_endicia_shipping,
            })
            # This is needed to update the shipment cost with
            # the carrier on sale else the shipment cost on
            # shipment will be generated from the default value
            for shipment in shipments:
                with Transaction().set_context(shipment.get_carrier_context()):
                    shipment_cost = self.carrier.get_sale_price()
                Shipment.write([shipment], {'cost': shipment_cost[0]})
        return shipments

    def get_endicia_shipping_cost(self):
        """Returns the calculated shipping cost as sent by endicia

        :returns: The shipping cost in USD
        """
        endicia_credentials = self.company.get_endicia_credentials()

        if not self.endicia_mailclass:
            self.raise_user_error('mailclass_missing')

        calculate_postage_request = CalculatingPostageAPI(
            mailclass = self.endicia_mailclass.value,
            weightoz = sum(map(
                lambda line: line.get_weight_for_endicia(), self.lines
            )),
            from_postal_code = self.warehouse.address.zip,
            to_postal_code = self.shipment_address.zip,
            to_country_code = self.shipment_address.country.code,
            accountid = endicia_credentials.account_id,
            requesterid = endicia_credentials.requester_id,
            passphrase = endicia_credentials.passphrase,
            test = endicia_credentials.usps_test,
        )

        response = calculate_postage_request.send_request()

        return Decimal(
            objectify_response(response).PostagePrice.get('TotalAmount')
        )


class SaleLine:
    'Sale Line'
    __name__ = 'sale.line'

    @classmethod
    def __setup__(cls):
        super(SaleLine, cls).__setup__()
        cls._error_messages.update({
            'weight_required': 'Weight is missing on the product %s',
        })

    def get_weight_for_endicia(self):
        """
        Returns weight as required for endicia.
        """
        ProductUom = Pool().get('product.uom')

        if self.product.type == 'service':
            return 0

        if not self.product.weight:
            self.raise_user_error(
                'weight_required',
                error_args=(self.product.name,)
            )

        # Find the quantity in the default uom of the product as the weight
        # is for per unit in that uom
        if self.unit != self.product.default_uom:
            quantity = ProductUom.compute_qty(
                self.unit,
                self.quantity,
                self.product.default_uom
            )
        else:
            quantity = self.quantity

        weight = float(self.product.weight) * quantity

        # Endicia by default uses oz for weight purposes
        if self.product.weight_uom.symbol != 'oz':
            ounce, = ProductUom.search([('symbol', '=', 'oz')])
            weight = ProductUom.compute_qty(
                self.product.weight_uom,
                weight,
                ounce
            )
        return math.ceil(weight)
