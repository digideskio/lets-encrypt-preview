"""Nginx Configuration"""
import logging
import os
import re
import shutil
import socket
import subprocess
import sys

import zope.interface

from acme import challenges

from letsencrypt import achallenges
from letsencrypt import constants as core_constants
from letsencrypt import errors
from letsencrypt import interfaces
from letsencrypt import le_util
from letsencrypt import reverter

from letsencrypt.plugins import common

from letsencrypt_nginx import constants
from letsencrypt_nginx import dvsni
from letsencrypt_nginx import obj
from letsencrypt_nginx import parser


class NginxConfigurator(common.Plugin):
    # pylint: disable=too-many-instance-attributes,too-many-public-methods
    """Nginx configurator.

    .. todo:: Add proper support for comments in the config. Currently,
        config files modified by the configurator will lose all their comments.

    :ivar config: Configuration.
    :type config: :class:`~letsencrypt.interfaces.IConfig`

    :ivar parser: Handles low level parsing
    :type parser: :class:`~letsencrypt_nginx.parser`

    :ivar str save_notes: Human-readable config change notes

    :ivar reverter: saves and reverts checkpoints
    :type reverter: :class:`letsencrypt.reverter.Reverter`

    :ivar tup version: version of Nginx

    """
    zope.interface.implements(interfaces.IAuthenticator, interfaces.IInstaller)
    zope.interface.classProvides(interfaces.IPluginFactory)

    description = "Nginx Web Server"

    @classmethod
    def add_parser_arguments(cls, add):
        add("server-root", default=constants.CLI_DEFAULTS["server_root"],
            help="Nginx server root directory.")
        add("mod-ssl-conf", default=constants.CLI_DEFAULTS["mod_ssl_conf"],
            help="Contains standard nginx SSL directives.")
        add("ctl", default=constants.CLI_DEFAULTS["ctl"], help="Path to the "
            "'nginx' binary, used for 'configtest' and retrieving nginx "
            "version number.")

    def __init__(self, *args, **kwargs):
        """Initialize an Nginx Configurator.

        :param tup version: version of Nginx as a tuple (1, 4, 7)
            (used mostly for unittesting)

        """
        version = kwargs.pop("version", None)
        super(NginxConfigurator, self).__init__(*args, **kwargs)

        # Verify that all directories and files exist with proper permissions
        if os.geteuid() == 0:
            self._verify_setup()

        # Files to save
        self.save_notes = ""

        # Add number of outstanding challenges
        self._chall_out = 0

        # These will be set in the prepare function
        self.parser = None
        self.version = version
        self._enhance_func = {}  # TODO: Support at least redirects

        # Set up reverter
        self.reverter = reverter.Reverter(self.config)
        self.reverter.recovery_routine()

    # This is called in determine_authenticator and determine_installer
    def prepare(self):
        """Prepare the authenticator/installer."""
        self.parser = parser.NginxParser(
            self.conf('server-root'),
            self.conf('mod-ssl-conf'))

        # Set Version
        if self.version is None:
            self.version = self.get_version()

        temp_install(self.conf('mod-ssl-conf'))

    # Entry point in main.py for installing cert
    def deploy_cert(self, domain, cert_path, key_path, chain_path=None):
        # pylint: disable=unused-argument
        """Deploys certificate to specified virtual host.

        .. note:: Aborts if the vhost is missing ssl_certificate or
            ssl_certificate_key.

        .. note:: Nginx doesn't have a cert chain directive, so the last
            parameter is always ignored. It expects the cert file to have
            the concatenated chain.

        .. note:: This doesn't save the config files!

        """
        vhost = self.choose_vhost(domain)
        directives = [['ssl_certificate', cert_path],
                      ['ssl_certificate_key', key_path]]

        try:
            self.parser.add_server_directives(vhost.filep, vhost.names,
                                              directives, True)
            logging.info("Deployed Certificate to VirtualHost %s for %s",
                         vhost.filep, vhost.names)
        except errors.LetsEncryptMisconfigurationError:
            logging.warn(
                "Cannot find a cert or key directive in %s for %s. "
                "VirtualHost was not modified.", vhost.filep, vhost.names)
            # Presumably break here so that the virtualhost is not modified
            return False

        self.save_notes += ("Changed vhost at %s with addresses of %s\n" %
                            (vhost.filep,
                             ", ".join(str(addr) for addr in vhost.addrs)))
        self.save_notes += "\tssl_certificate %s\n" % cert_path
        self.save_notes += "\tssl_certificate_key %s\n" % key_path

    #######################
    # Vhost parsing methods
    #######################
    def choose_vhost(self, target_name):
        """Chooses a virtual host based on the given domain name.

        .. note:: This makes the vhost SSL-enabled if it isn't already. Follows
            Nginx's server block selection rules preferring blocks that are
            already SSL.

        .. todo:: This should maybe return list if no obvious answer
            is presented.

        .. todo:: The special name "$hostname" corresponds to the machine's
            hostname. Currently we just ignore this.

        :param str target_name: domain name

        :returns: ssl vhost associated with name
        :rtype: :class:`~letsencrypt_nginx.obj.VirtualHost`

        """
        vhost = None

        matches = self._get_ranked_matches(target_name)
        if not matches:
            # No matches. Create a new vhost with this name in nginx.conf.
            filep = self.parser.loc["root"]
            new_block = [['server'], [['server_name', target_name]]]
            self.parser.add_http_directives(filep, new_block)
            vhost = obj.VirtualHost(filep, set([]), False, True,
                                    set([target_name]), list(new_block[1]))
        elif matches[0]['rank'] in xrange(2, 6):
            # Wildcard match - need to find the longest one
            rank = matches[0]['rank']
            wildcards = [x for x in matches if x['rank'] == rank]
            vhost = max(wildcards, key=lambda x: len(x['name']))['vhost']
        else:
            vhost = matches[0]['vhost']

        if vhost is not None:
            if not vhost.ssl:
                self._make_server_ssl(vhost)

        return vhost

    def _get_ranked_matches(self, target_name):
        """Returns a ranked list of vhosts that match target_name.

        :param str target_name: The name to match
        :returns: list of dicts containing the vhost, the matching name, and
            the numerical rank
        :rtype: list

        """
        # Nginx chooses a matching server name for a request with precedence:
        # 1. exact name match
        # 2. longest wildcard name starting with *
        # 3. longest wildcard name ending with *
        # 4. first matching regex in order of appearance in the file
        matches = []
        for vhost in self.parser.get_vhosts():
            name_type, name = parser.get_best_match(target_name, vhost.names)
            if name_type == 'exact':
                matches.append({'vhost': vhost,
                                'name': name,
                                'rank': 0 if vhost.ssl else 1})
            elif name_type == 'wildcard_start':
                matches.append({'vhost': vhost,
                                'name': name,
                                'rank': 2 if vhost.ssl else 3})
            elif name_type == 'wildcard_end':
                matches.append({'vhost': vhost,
                                'name': name,
                                'rank': 4 if vhost.ssl else 5})
            elif name_type == 'regex':
                matches.append({'vhost': vhost,
                                'name': name,
                                'rank': 6 if vhost.ssl else 7})
        return sorted(matches, key=lambda x: x['rank'])

    def get_all_names(self):
        """Returns all names found in the Nginx Configuration.

        :returns: All ServerNames, ServerAliases, and reverse DNS entries for
                  virtual host addresses
        :rtype: set

        """
        all_names = set()

        # Kept in same function to avoid multiple compilations of the regex
        priv_ip_regex = (r"(^127\.0\.0\.1)|(^10\.)|(^172\.1[6-9]\.)|"
                         r"(^172\.2[0-9]\.)|(^172\.3[0-1]\.)|(^192\.168\.)")
        private_ips = re.compile(priv_ip_regex)
        hostname_regex = r"^(([a-z0-9]|[a-z0-9][a-z0-9\-]*[a-z0-9])\.)*[a-z]+$"
        hostnames = re.compile(hostname_regex, re.IGNORECASE)

        for vhost in self.parser.get_vhosts():
            all_names.update(vhost.names)

            for addr in vhost.addrs:
                host = addr.get_addr()
                if hostnames.match(host):
                    # If it's a hostname, add it to the names.
                    all_names.add(host)
                elif not private_ips.match(host):
                    # If it isn't a private IP, do a reverse DNS lookup
                    # TODO: IPv6 support
                    try:
                        socket.inet_aton(host)
                        all_names.add(socket.gethostbyaddr(host)[0])
                    except (socket.error, socket.herror, socket.timeout):
                        continue

        return all_names

    def _make_server_ssl(self, vhost):
        """Makes a server SSL based on server_name and filename by adding
        a 'listen 443 ssl' directive to the server block.

        .. todo:: Maybe this should create a new block instead of modifying
            the existing one?

        :param vhost: The vhost to add SSL to.
        :type vhost: :class:`~letsencrypt_nginx.obj.VirtualHost`

        """
        ssl_block = [['listen', '443 ssl'],
                     ['ssl_certificate',
                      '/etc/ssl/certs/ssl-cert-snakeoil.pem'],
                     ['ssl_certificate_key',
                      '/etc/ssl/private/ssl-cert-snakeoil.key'],
                     ['include', self.parser.loc["ssl_options"]]]
        self.parser.add_server_directives(
            vhost.filep, vhost.names, ssl_block)
        vhost.ssl = True
        vhost.raw.extend(ssl_block)
        vhost.addrs.add(obj.Addr('', '443', True, False))

    def get_all_certs_keys(self):
        """Find all existing keys, certs from configuration.

        :returns: list of tuples with form [(cert, key, path)]
            cert - str path to certificate file
            key - str path to associated key file
            path - File path to configuration file.
        :rtype: set

        """
        return self.parser.get_all_certs_keys()

    ##################################
    # enhancement methods (IInstaller)
    ##################################
    def supported_enhancements(self):  # pylint: disable=no-self-use
        """Returns currently supported enhancements."""
        return []

    def enhance(self, domain, enhancement, options=None):
        """Enhance configuration.

        :param str domain: domain to enhance
        :param str enhancement: enhancement type defined in
            :const:`~letsencrypt.constants.ENHANCEMENTS`
        :param options: options for the enhancement
            See :const:`~letsencrypt.constants.ENHANCEMENTS`
            documentation for appropriate parameter.

        """
        try:
            return self._enhance_func[enhancement](
                self.choose_vhost(domain), options)
        except (KeyError, ValueError):
            raise errors.LetsEncryptConfiguratorError(
                "Unsupported enhancement: {0}".format(enhancement))
        except errors.LetsEncryptConfiguratorError:
            logging.warn("Failed %s for %s", enhancement, domain)

    ######################################
    # Nginx server management (IInstaller)
    ######################################
    def restart(self):
        """Restarts nginx server.

        :returns: Success
        :rtype: bool

        """
        return nginx_restart(self.conf('ctl'))

    def config_test(self):  # pylint: disable=no-self-use
        """Check the configuration of Nginx for errors.

        :returns: Success
        :rtype: bool

        """
        try:
            proc = subprocess.Popen(
                [self.conf('ctl'), "-t"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
            stdout, stderr = proc.communicate()
        except (OSError, ValueError):
            logging.fatal("Unable to run nginx config test")
            sys.exit(1)

        if proc.returncode != 0:
            # Enter recovery routine...
            logging.error("Config test failed\n%s\n%s", stdout, stderr)
            return False

        return True

    def _verify_setup(self):
        """Verify the setup to ensure safe operating environment.

        Make sure that files/directories are setup with appropriate permissions
        Aim for defensive coding... make sure all input files
        have permissions of root.

        """
        uid = os.geteuid()
        le_util.make_or_verify_dir(
            self.config.work_dir, core_constants.CONFIG_DIRS_MODE, uid)
        le_util.make_or_verify_dir(
            self.config.backup_dir, core_constants.CONFIG_DIRS_MODE, uid)
        le_util.make_or_verify_dir(
            self.config.config_dir, core_constants.CONFIG_DIRS_MODE, uid)

    def get_version(self):
        """Return version of Nginx Server.

        Version is returned as tuple. (ie. 2.4.7 = (2, 4, 7))

        :returns: version
        :rtype: tuple

        :raises errors.LetsEncryptConfiguratorError:
            Unable to find Nginx version or version is unsupported

        """
        try:
            proc = subprocess.Popen(
                [self.conf('ctl'), "-V"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
            text = proc.communicate()[1]  # nginx prints output to stderr
        except (OSError, ValueError):
            raise errors.LetsEncryptConfiguratorError(
                "Unable to run %s -V" % self.conf('ctl'))

        version_regex = re.compile(r"nginx/([0-9\.]*)", re.IGNORECASE)
        version_matches = version_regex.findall(text)

        sni_regex = re.compile(r"TLS SNI support enabled", re.IGNORECASE)
        sni_matches = sni_regex.findall(text)

        ssl_regex = re.compile(r" --with-http_ssl_module")
        ssl_matches = ssl_regex.findall(text)

        if not version_matches:
            raise errors.LetsEncryptConfiguratorError(
                "Unable to find Nginx version")
        if not ssl_matches:
            raise errors.LetsEncryptConfiguratorError(
                "Nginx build is missing SSL module (--with-http_ssl_module).")
        if not sni_matches:
            raise errors.LetsEncryptConfiguratorError(
                "Nginx build doesn't support SNI")

        nginx_version = tuple([int(i) for i in version_matches[0].split(".")])

        # nginx < 0.8.48 uses machine hostname as default server_name instead of
        # the empty string
        if nginx_version < (0, 8, 48):
            raise errors.LetsEncryptConfiguratorError(
                "Nginx version must be 0.8.48+")

        return nginx_version

    def more_info(self):
        """Human-readable string to help understand the module"""
        return (
            "Configures Nginx to authenticate and install HTTPS.{0}"
            "Server root: {root}{0}"
            "Version: {version}".format(
                os.linesep, root=self.parser.loc["root"],
                version=".".join(str(i) for i in self.version))
        )

    ###################################################
    # Wrapper functions for Reverter class (IInstaller)
    ###################################################
    def save(self, title=None, temporary=False):
        """Saves all changes to the configuration files.

        :param str title: The title of the save. If a title is given, the
            configuration will be saved as a new checkpoint and put in a
            timestamped directory.

        :param bool temporary: Indicates whether the changes made will
            be quickly reversed in the future (ie. challenges)

        """
        save_files = set(self.parser.parsed.keys())

        # Create Checkpoint
        if temporary:
            self.reverter.add_to_temp_checkpoint(
                save_files, self.save_notes)
        else:
            self.reverter.add_to_checkpoint(save_files,
                                            self.save_notes)

        # Change 'ext' to something else to not override existing conf files
        self.parser.filedump(ext='')
        if title and not temporary:
            self.reverter.finalize_checkpoint(title)

        return True

    def recovery_routine(self):
        """Revert all previously modified files.

        Reverts all modified files that have not been saved as a checkpoint

        """
        self.reverter.recovery_routine()
        self.parser.load()

    def revert_challenge_config(self):
        """Used to cleanup challenge configurations."""
        self.reverter.revert_temporary_config()
        self.parser.load()

    def rollback_checkpoints(self, rollback=1):
        """Rollback saved checkpoints.

        :param int rollback: Number of checkpoints to revert

        """
        self.reverter.rollback_checkpoints(rollback)
        self.parser.load()

    def view_config_changes(self):
        """Show all of the configuration changes that have taken place."""
        self.reverter.view_config_changes()

    ###########################################################################
    # Challenges Section for IAuthenticator
    ###########################################################################
    def get_chall_pref(self, unused_domain):  # pylint: disable=no-self-use
        """Return list of challenge preferences."""
        return [challenges.DVSNI]

    # Entry point in main.py for performing challenges
    def perform(self, achalls):
        """Perform the configuration related challenge.

        This function currently assumes all challenges will be fulfilled.
        If this turns out not to be the case in the future. Cleanup and
        outstanding challenges will have to be designed better.

        """
        self._chall_out += len(achalls)
        responses = [None] * len(achalls)
        nginx_dvsni = dvsni.NginxDvsni(self)

        for i, achall in enumerate(achalls):
            if isinstance(achall, achallenges.DVSNI):
                # Currently also have dvsni hold associated index
                # of the challenge. This helps to put all of the responses back
                # together when they are all complete.
                nginx_dvsni.add_chall(achall, i)

        sni_response = nginx_dvsni.perform()
        # Must restart in order to activate the challenges.
        # Handled here because we may be able to load up other challenge types
        self.restart()

        # Go through all of the challenges and assign them to the proper place
        # in the responses return value. All responses must be in the same order
        # as the original challenges.
        for i, resp in enumerate(sni_response):
            responses[nginx_dvsni.indices[i]] = resp

        return responses

    # called after challenges are performed
    def cleanup(self, achalls):
        """Revert all challenges."""
        self._chall_out -= len(achalls)

        # If all of the challenges have been finished, clean up everything
        if self._chall_out <= 0:
            self.revert_challenge_config()
            self.restart()


def nginx_restart(nginx_ctl):
    """Restarts the Nginx Server.

    .. todo:: Nginx restart is fatal if the configuration references
        non-existent SSL cert/key files. Remove references to /etc/letsencrypt
        before restart.

    :param str nginx_ctl: Path to the Nginx binary.

    """
    try:
        proc = subprocess.Popen([nginx_ctl, "-s", "reload"],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        stdout, stderr = proc.communicate()

        if proc.returncode != 0:
            # Maybe Nginx isn't running
            nginx_proc = subprocess.Popen([nginx_ctl],
                                          stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE)
            stdout, stderr = nginx_proc.communicate()

            if nginx_proc.returncode != 0:
                # Enter recovery routine...
                logging.error("Nginx Restart Failed!\n%s\n%s", stdout, stderr)
                return False

    except (OSError, ValueError):
        logging.fatal("Nginx Restart Failed - Please Check the Configuration")
        sys.exit(1)

    return True


def temp_install(options_ssl):
    """Temporary install for convenience."""
    # WARNING: THIS IS A POTENTIAL SECURITY VULNERABILITY
    # THIS SHOULD BE HANDLED BY THE PACKAGE MANAGER
    # AND TAKEN OUT BEFORE RELEASE, INSTEAD
    # SHOWING A NICE ERROR MESSAGE ABOUT THE PROBLEM.

    # Check to make sure options-ssl.conf is installed
    if not os.path.isfile(options_ssl):
        shutil.copyfile(constants.MOD_SSL_CONF, options_ssl)
