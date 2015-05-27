from collections import OrderedDict
import datetime
from hashlib import md5

from flask import current_app
from flask_login import UserMixin
from itsdangerous import URLSafeTimedSerializer, \
    TimedJSONWebSignatureSerializer
from sqlalchemy import or_

from catwatch.lib.util_sqlalchemy import ResourceMixin
from catwatch.blueprints.billing.models import CreditCard, Subscription, \
    Invoice
from catwatch.extensions import db, bcrypt


class User(UserMixin, ResourceMixin, db.Model):
    ROLE = OrderedDict([
        ('guest', 'Guest'),
        ('member', 'Member'),
        ('admin', 'Admin')
    ])

    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)

    # Relationships.
    credit_card = db.relationship(CreditCard, uselist=False, backref='users',
                                  passive_deletes=True)
    subscription = db.relationship(Subscription, uselist=False,
                                   backref='users', passive_deletes=True)
    invoices = db.relationship(Invoice, backref='users')

    # Authentication.
    role = db.Column(db.Enum(*ROLE, name='role_types'),
                     index=True, nullable=False, server_default='member')
    active = db.Column('is_active', db.Boolean(), nullable=False,
                       server_default='1')
    username = db.Column(db.String(24), unique=True, index=True)
    email = db.Column(db.String(255), unique=True, index=True, nullable=False,
                      server_default='')
    password = db.Column(db.String(128), nullable=False, server_default='')

    # Billing.
    name = db.Column(db.String(128), index=True)
    stripe_customer_id = db.Column(db.String(128), index=True)
    cancelled_subscription_on = db.Column(db.DateTime)

    # Activity tracking.
    sign_in_count = db.Column(db.Integer, nullable=False, default=0)
    current_sign_in_on = db.Column(db.DateTime)
    current_sign_in_ip = db.Column(db.String(45))
    last_sign_in_on = db.Column(db.DateTime)
    last_sign_in_ip = db.Column(db.String(45))

    def __init__(self, **kwargs):
        # Call Flask-SQLAlchemy's constructor.
        super(User, self).__init__(**kwargs)

        # Transform a few fields.
        self.username = kwargs.get('username', None)
        if self.username is not None:
            self.username = self.username.lower()

        self.password = User.encrypt_password(kwargs.get('password', ''))

    @classmethod
    def search(cls, query, fields):
        """
        Search a resource by 1 or more fields.

        :param query: Search query
        :type query: str
        :param fields: Fields to search
        :type fields: tuple
        :return: SQLAlchemy filter
        """
        if not query:
            return ''

        # TODO: Refactor this to dynamically search on any model by any filter.
        search_query = '%{0}%'.format(query)
        search_chain = (User.email.ilike(search_query),
                        User.name.ilike(search_query))

        return or_(*search_chain)

    @classmethod
    def find_by_identity(cls, identity):
        """
        Find a user by their e-mail or username.

        :param identity: Email or username
        :type identity: str
        :return: A user instance
        """
        return User.query.filter((User.email == identity)
                                 | (User.username == identity)).first()

    @classmethod
    def encrypt_password(cls, plaintext_password):
        """
        Hash a plaintext string using bcrypt.

        :param plaintext_password: A password
        :type plaintext_password: str
        :return: str
        """
        if plaintext_password:
            return bcrypt.generate_password_hash(plaintext_password, 8)

        return None

    @classmethod
    def deserialize_token(cls, token):
        """
        Obtain a user from de-serializing a signed token.

        :param token: A signed token
        :type token: str
        :return: A user instance or None
        """
        private_key = TimedJSONWebSignatureSerializer(
            current_app.config['SECRET_KEY'])
        try:
            decoded_payload = private_key.loads(token)

            return User.find_by_identity(decoded_payload.get('user_email'))
        except Exception:
            return None

    @classmethod
    def is_last_admin(cls, user, new_role, new_active):
        """
        Determine whether or not this user is the last admin account.

        :param user: User being tested
        :type user: User
        :param new_role: New role being set
        :type new_role: str
        :param new_active: New active status being set
        :type new_active: bool
        :return: bool
        """
        is_changing_roles = user.role == 'admin' and new_role != 'admin'
        is_changing_active = user.active is True and new_active is None

        if is_changing_roles or is_changing_active:
            admin_count = User.query.filter(User.role == 'admin').count()
            active_count = User.query.filter(User.is_active is True).count()
            if admin_count == 1 or active_count == 1:
                return True

        return False

    def is_active(self):
        """
        Return whether or not the user account is active, this satisfies
        Flask-Login by overwriting the default value.

        :return: bool
        """
        return self.active

    def get_auth_token(self):
        """
        Return the user's auth token. Use their password as part of the token
        because if the user changes their password we will want to invalidate
        all of their logins across devices. It is completely fine to use
        # md5 here as nothing leaks.

        # This satisfies Flask-Login by providing a means to create a token.

        :return: str
        """
        private_key = current_app.config['SECRET_KEY']

        serializer = URLSafeTimedSerializer(private_key)
        data = [str(self.id), md5(self.password).hexdigest()]

        return serializer.dumps(data).decode('utf-8')

    def authenticated(self, with_password=True, password=''):
        """
        Ensure a user is authenticated, and optionally checking their password.

        :param with_password: Optionally check their password
        :type with_password: bool
        :param password: Optionally verify this as their password
        :type password: str
        :return: bool
        """
        if with_password:
            return bcrypt.check_password_hash(self.password, password)

        return True

    def serialize_token(self, expiration=3600):
        """
        Sign and create a token that can be used for things such as resetting
        a password or other tasks that involve a one off token.

        :param expiration: seconds until it expires, defaults to 1 hour
        :type expiration: int
        :return: JSON
        """
        private_key = current_app.config['SECRET_KEY']

        serializer = TimedJSONWebSignatureSerializer(private_key, expiration)
        return serializer.dumps({'user_email': self.email}).decode('utf-8')

    def update_activity_tracking(self, ip_address):
        """
        Update various fields on the user that's related to meta data on his
        account, such as the sign in count and ip address, etc..

        :param ip_address: IP address
        :type ip_address: str
        :return: The result of updating the record
        """
        self.sign_in_count += 1

        self.last_sign_in_on = self.current_sign_in_on
        self.last_sign_in_ip = self.current_sign_in_ip

        self.current_sign_in_on = datetime.datetime.utcnow()
        self.current_sign_in_ip = ip_address

        return db.session.commit()
