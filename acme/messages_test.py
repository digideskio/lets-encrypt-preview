"""Tests for acme.messages."""
import os
import pkg_resources
import unittest

from Crypto.PublicKey import RSA
import M2Crypto
import mock

from acme import challenges
from acme import jose


CERT = jose.ComparableX509(M2Crypto.X509.load_cert_string(
    pkg_resources.resource_string(
        'acme.jose', os.path.join('testdata', 'cert.der')),
    M2Crypto.X509.FORMAT_DER))
CSR = jose.ComparableX509(M2Crypto.X509.load_request_string(
    pkg_resources.resource_string(
        'acme.jose', os.path.join('testdata', 'csr.der')),
    M2Crypto.X509.FORMAT_DER))
KEY = jose.util.HashableRSAKey(RSA.importKey(pkg_resources.resource_string(
    'acme.jose', os.path.join('testdata', 'rsa512_key.pem'))))
CERT = jose.ComparableX509(M2Crypto.X509.load_cert(
    format=M2Crypto.X509.FORMAT_DER, file=pkg_resources.resource_filename(
        'acme.jose', os.path.join('testdata', 'cert.der'))))


class ErrorTest(unittest.TestCase):
    """Tests for acme.messages.Error."""

    def setUp(self):
        from acme.messages import Error
        self.error = Error(detail='foo', typ='malformed', title='title')
        self.jobj = {'detail': 'foo', 'title': 'some title'}

    def test_typ_prefix(self):
        self.assertEqual('malformed', self.error.typ)
        self.assertEqual(
            'urn:acme:error:malformed', self.error.to_partial_json()['type'])
        self.assertEqual(
            'malformed', self.error.from_json(self.error.to_partial_json()).typ)

    def test_typ_decoder_missing_prefix(self):
        from acme.messages import Error
        self.jobj['type'] = 'malformed'
        self.assertRaises(jose.DeserializationError, Error.from_json, self.jobj)
        self.jobj['type'] = 'not valid bare type'
        self.assertRaises(jose.DeserializationError, Error.from_json, self.jobj)

    def test_typ_decoder_not_recognized(self):
        from acme.messages import Error
        self.jobj['type'] = 'urn:acme:error:baz'
        self.assertRaises(jose.DeserializationError, Error.from_json, self.jobj)

    def test_description(self):
        self.assertEqual(
            'The request message was malformed', self.error.description)

    def test_from_json_hashable(self):
        from acme.messages import Error
        hash(Error.from_json(self.error.to_json()))

    def test_str(self):
        self.assertEqual(
            'malformed :: The request message was malformed :: foo',
            str(self.error))
        self.assertEqual('foo', str(self.error.update(typ=None)))


class ConstantTest(unittest.TestCase):
    """Tests for acme.messages._Constant."""

    def setUp(self):
        from acme.messages import _Constant
        class MockConstant(_Constant):  # pylint: disable=missing-docstring
            POSSIBLE_NAMES = {}

        self.MockConstant = MockConstant  # pylint: disable=invalid-name
        self.const_a = MockConstant('a')
        self.const_b = MockConstant('b')

    def test_to_partial_json(self):
        self.assertEqual('a', self.const_a.to_partial_json())
        self.assertEqual('b', self.const_b.to_partial_json())

    def test_from_json(self):
        self.assertEqual(self.const_a, self.MockConstant.from_json('a'))
        self.assertRaises(
            jose.DeserializationError, self.MockConstant.from_json, 'c')

    def test_from_json_hashable(self):
        hash(self.MockConstant.from_json('a'))

    def test_repr(self):
        self.assertEqual('MockConstant(a)', repr(self.const_a))
        self.assertEqual('MockConstant(b)', repr(self.const_b))

    def test_equality(self):
        const_a_prime = self.MockConstant('a')
        self.assertFalse(self.const_a == self.const_b)
        self.assertTrue(self.const_a == const_a_prime)

        self.assertTrue(self.const_a != self.const_b)
        self.assertFalse(self.const_a != const_a_prime)


class RegistrationTest(unittest.TestCase):
    """Tests for acme.messages.Registration."""

    def setUp(self):
        key = jose.jwk.JWKRSA(key=KEY.publickey())
        contact = (
            'mailto:admin@foo.com',
            'tel:1234',
        )
        recovery_token = 'XYZ'
        agreement = 'https://letsencrypt.org/terms'

        from acme.messages import Registration
        self.reg = Registration(
            key=key, contact=contact, recovery_token=recovery_token,
            agreement=agreement)

        self.jobj_to = {
            'contact': contact,
            'recoveryToken': recovery_token,
            'agreement': agreement,
            'key': key,
        }
        self.jobj_from = self.jobj_to.copy()
        self.jobj_from['key'] = key.to_json()

    def test_from_data(self):
        from acme.messages import Registration
        reg = Registration.from_data(phone='1234', email='admin@foo.com')
        self.assertEqual(reg.contact, (
            'tel:1234',
            'mailto:admin@foo.com',
        ))

    def test_phones(self):
        self.assertEqual(('1234',), self.reg.phones)

    def test_emails(self):
        self.assertEqual(('admin@foo.com',), self.reg.emails)

    def test_phone(self):
        self.assertEqual('1234', self.reg.phone)

    def test_email(self):
        self.assertEqual('admin@foo.com', self.reg.email)

    def test_to_partial_json(self):
        self.assertEqual(self.jobj_to, self.reg.to_partial_json())

    def test_from_json(self):
        from acme.messages import Registration
        self.assertEqual(self.reg, Registration.from_json(self.jobj_from))

    def test_from_json_hashable(self):
        from acme.messages import Registration
        hash(Registration.from_json(self.jobj_from))


class RegistrationResourceTest(unittest.TestCase):
    """Tests for acme.messages.RegistrationResource."""

    def setUp(self):
        from acme.messages import RegistrationResource
        self.regr = RegistrationResource(
            body=mock.sentinel.body, uri=mock.sentinel.uri,
            new_authzr_uri=mock.sentinel.new_authzr_uri,
            terms_of_service=mock.sentinel.terms_of_service)

    def test_to_partial_json(self):
        self.assertEqual(self.regr.to_json(), {
            'body': mock.sentinel.body,
            'uri': mock.sentinel.uri,
            'new_authzr_uri': mock.sentinel.new_authzr_uri,
            'terms_of_service': mock.sentinel.terms_of_service,
        })


class ChallengeResourceTest(unittest.TestCase):
    """Tests for acme.messages.ChallengeResource."""

    def test_uri(self):
        from acme.messages import ChallengeResource
        self.assertEqual('http://challb', ChallengeResource(body=mock.MagicMock(
            uri='http://challb'), authzr_uri='http://authz').uri)


class ChallengeBodyTest(unittest.TestCase):
    """Tests for acme.messages.ChallengeBody."""

    def setUp(self):
        self.chall = challenges.DNS(token='foo')

        from acme.messages import ChallengeBody
        from acme.messages import STATUS_VALID
        self.status = STATUS_VALID
        self.challb = ChallengeBody(
            uri='http://challb', chall=self.chall, status=self.status)

        self.jobj_to = {
            'uri': 'http://challb',
            'status': self.status,
            'type': 'dns',
            'token': 'foo',
        }
        self.jobj_from = self.jobj_to.copy()
        self.jobj_from['status'] = 'valid'

    def test_to_partial_json(self):
        self.assertEqual(self.jobj_to, self.challb.to_partial_json())

    def test_from_json(self):
        from acme.messages import ChallengeBody
        self.assertEqual(self.challb, ChallengeBody.from_json(self.jobj_from))

    def test_from_json_hashable(self):
        from acme.messages import ChallengeBody
        hash(ChallengeBody.from_json(self.jobj_from))

    def test_proxy(self):
        self.assertEqual('foo', self.challb.token)


class AuthorizationTest(unittest.TestCase):
    """Tests for acme.messages.Authorization."""

    def setUp(self):
        from acme.messages import ChallengeBody
        from acme.messages import STATUS_VALID
        self.challbs = (
            ChallengeBody(
                uri='http://challb1', status=STATUS_VALID,
                chall=challenges.SimpleHTTP(token='IlirfxKKXAsHtmzK29Pj8A')),
            ChallengeBody(uri='http://challb2', status=STATUS_VALID,
                          chall=challenges.DNS(token='DGyRejmCefe7v4NfDGDKfA')),
            ChallengeBody(uri='http://challb3', status=STATUS_VALID,
                          chall=challenges.RecoveryToken()),
        )
        combinations = ((0, 2), (1, 2))

        from acme.messages import Authorization
        from acme.messages import Identifier
        from acme.messages import IDENTIFIER_FQDN
        identifier = Identifier(typ=IDENTIFIER_FQDN, value='example.com')
        self.authz = Authorization(
            identifier=identifier, combinations=combinations,
            challenges=self.challbs)

        self.jobj_from = {
            'identifier': identifier.to_json(),
            'challenges': [challb.to_json() for challb in self.challbs],
            'combinations': combinations,
        }

    def test_from_json(self):
        from acme.messages import Authorization
        Authorization.from_json(self.jobj_from)

    def test_from_json_hashable(self):
        from acme.messages import Authorization
        hash(Authorization.from_json(self.jobj_from))

    def test_resolved_combinations(self):
        self.assertEqual(self.authz.resolved_combinations, (
            (self.challbs[0], self.challbs[2]),
            (self.challbs[1], self.challbs[2]),
        ))


class AuthorizationResourceTest(unittest.TestCase):
    """Tests for acme.messages.AuthorizationResource."""

    def test_json_de_serializable(self):
        from acme.messages import AuthorizationResource
        authzr = AuthorizationResource(
            uri=mock.sentinel.uri,
            body=mock.sentinel.body,
            new_cert_uri=mock.sentinel.new_cert_uri,
        )
        self.assertTrue(isinstance(authzr, jose.JSONDeSerializable))


class CertificateRequestTest(unittest.TestCase):
    """Tests for acme.messages.CertificateRequest."""

    def setUp(self):
        from acme.messages import CertificateRequest
        self.req = CertificateRequest(csr=CSR, authorizations=('foo',))

    def test_json_de_serializable(self):
        self.assertTrue(isinstance(self.req, jose.JSONDeSerializable))
        from acme.messages import CertificateRequest
        self.assertEqual(
            self.req, CertificateRequest.from_json(self.req.to_json()))


class CertificateResourceTest(unittest.TestCase):
    """Tests for acme.messages.CertificateResourceTest."""

    def setUp(self):
        from acme.messages import CertificateResource
        self.certr = CertificateResource(
            body=CERT, uri=mock.sentinel.uri, authzrs=(),
            cert_chain_uri=mock.sentinel.cert_chain_uri)

    def test_json_de_serializable(self):
        self.assertTrue(isinstance(self.certr, jose.JSONDeSerializable))
        from acme.messages import CertificateResource
        self.assertEqual(
            self.certr, CertificateResource.from_json(self.certr.to_json()))


class RevocationTest(unittest.TestCase):
    """Tests for acme.messages.RevocationTest."""

    def test_url(self):
        from acme.messages import Revocation
        url = 'https://letsencrypt-demo.org/acme/revoke-cert'
        self.assertEqual(url, Revocation.url('https://letsencrypt-demo.org'))
        self.assertEqual(
            url, Revocation.url('https://letsencrypt-demo.org/acme/new-reg'))

    def setUp(self):
        from acme.messages import Revocation
        self.rev = Revocation(certificate=CERT)

    def test_from_json_hashable(self):
        from acme.messages import Revocation
        hash(Revocation.from_json(self.rev.to_json()))


if __name__ == '__main__':
    unittest.main()  # pragma: no cover
