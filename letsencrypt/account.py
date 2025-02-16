"""Creates ACME accounts for server."""
import logging
import os
import re

import configobj
import zope.component

from acme import messages

from letsencrypt import crypto_util
from letsencrypt import errors
from letsencrypt import interfaces
from letsencrypt import le_util

from letsencrypt.display import util as display_util


class Account(object):
    """ACME protocol registration.

    :ivar config: Client configuration object
    :type config: :class:`~letsencrypt.interfaces.IConfig`
    :ivar key: Account/Authorized Key
    :type key: :class:`~letsencrypt.le_util.Key`

    :ivar str email: Client's email address
    :ivar str phone: Client's phone number

    :ivar regr: Registration Resource
    :type regr: :class:`~acme.messages.RegistrationResource`

    """

    # Just make sure we don't get pwned
    # Make sure that it also doesn't start with a period or have two consecutive
    # periods <- this needs to be done in addition to the regex
    EMAIL_REGEX = re.compile("[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+$")

    def __init__(self, config, key, email=None, phone=None, regr=None):
        le_util.make_or_verify_dir(
            config.accounts_dir, 0o700, os.geteuid())
        self.key = key
        self.config = config
        if email is not None and self.safe_email(email):
            self.email = email
        else:
            self.email = None
        self.phone = phone

        self.regr = regr

    @property
    def uri(self):
        """URI link for new registrations."""
        if self.regr is not None:
            return self.regr.uri
        else:
            return None

    @property
    def new_authzr_uri(self):  # pylint: disable=missing-docstring
        if self.regr is not None:
            return self.regr.new_authzr_uri
        else:
            return None

    @property
    def terms_of_service(self):  # pylint: disable=missing-docstring
        if self.regr is not None:
            return self.regr.terms_of_service
        else:
            return None

    @property
    def recovery_token(self):  # pylint: disable=missing-docstring
        if self.regr is not None and self.regr.body is not None:
            return self.regr.body.recovery_token
        else:
            return None

    def save(self):
        """Save account to disk."""
        le_util.make_or_verify_dir(
            self.config.accounts_dir, 0o700, os.geteuid())

        acc_config = configobj.ConfigObj()
        acc_config.filename = os.path.join(
            self.config.accounts_dir, self._get_config_filename(self.email))

        acc_config.initial_comment = [
            "DO NOT EDIT THIS FILE",
            "Account information for %s under %s" % (
                self._get_config_filename(self.email), self.config.server),
        ]

        acc_config["key"] = self.key.file
        acc_config["phone"] = self.phone

        if self.regr is not None:
            acc_config["RegistrationResource"] = {}
            acc_config["RegistrationResource"]["uri"] = self.uri
            acc_config["RegistrationResource"]["new_authzr_uri"] = (
                self.new_authzr_uri)
            acc_config["RegistrationResource"]["terms_of_service"] = (
                self.terms_of_service)

            regr_dict = self.regr.body.to_json()
            acc_config["RegistrationResource"]["body"] = regr_dict

        acc_config.write()

    @classmethod
    def _get_config_filename(cls, email):
        return email if email is not None and email else "default"

    @classmethod
    def from_existing_account(cls, config, email=None):
        """Populate an account from an existing email."""
        config_fp = os.path.join(
            config.accounts_dir, cls._get_config_filename(email))
        return cls._from_config_fp(config, config_fp)

    @classmethod
    def _from_config_fp(cls, config, config_fp):
        try:
            acc_config = configobj.ConfigObj(
                infile=config_fp, file_error=True, create_empty=False)
        except IOError:
            raise errors.LetsEncryptClientError(
                "Account for %s does not exist" % os.path.basename(config_fp))

        if os.path.basename(config_fp) != "default":
            email = os.path.basename(config_fp)
        else:
            email = None
        phone = acc_config["phone"] if acc_config["phone"] != "None" else None

        with open(acc_config["key"]) as key_file:
            key = le_util.Key(acc_config["key"], key_file.read())

        if "RegistrationResource" in acc_config:
            acc_config_rr = acc_config["RegistrationResource"]
            regr = messages.RegistrationResource(
                uri=acc_config_rr["uri"],
                new_authzr_uri=acc_config_rr["new_authzr_uri"],
                terms_of_service=acc_config_rr["terms_of_service"],
                body=messages.Registration.from_json(acc_config_rr["body"]))
        else:
            regr = None

        return cls(config, key, email, phone, regr)

    @classmethod
    def get_accounts(cls, config):
        """Return all current accounts.

        :param config: Configuration
        :type config: :class:`letsencrypt.interfaces.IConfig`

        """
        try:
            filenames = os.listdir(config.accounts_dir)
        except OSError:
            return []

        accounts = []
        for name in filenames:
            # Not some directory ie. keys
            config_fp = os.path.join(config.accounts_dir, name)
            if os.path.isfile(config_fp):
                accounts.append(cls._from_config_fp(config, config_fp))

        return accounts

    @classmethod
    def from_prompts(cls, config):
        """Generate an account from prompted user input.

        :param config: Configuration
        :type config: :class:`letsencrypt.interfaces.IConfig`

        :returns: Account or None
        :rtype: :class:`letsencrypt.account.Account`

        """
        while True:
            code, email = zope.component.getUtility(interfaces.IDisplay).input(
                "Enter email address")

            if code == display_util.OK:
                try:
                    return cls.from_email(config, email)
                except errors.LetsEncryptClientError:
                    continue
            else:
                return None

    @classmethod
    def from_email(cls, config, email):
        """Generate a new account from an email address.

        :param config: Configuration
        :type config: :class:`letsencrypt.interfaces.IConfig`

        :param str email: Email address

        :raises letsencrypt.errors.LetsEncryptClientError: If invalid
            email address is given.

        """
        if not email or cls.safe_email(email):
            email = email if email else None

            le_util.make_or_verify_dir(
                config.account_keys_dir, 0o700, os.geteuid())
            key = crypto_util.init_save_key(
                config.rsa_key_size, config.account_keys_dir,
                cls._get_config_filename(email))
            return cls(config, key, email)

        raise errors.LetsEncryptClientError("Invalid email address.")

    @classmethod
    def safe_email(cls, email):
        """Scrub email address before using it."""
        if cls.EMAIL_REGEX.match(email):
            return not email.startswith(".") and ".." not in email
        else:
            logging.warn("Invalid email address: %s.", email)
            return False
