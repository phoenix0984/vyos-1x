#!/usr/bin/env python3
#
# Copyright (C) 2021 VyOS maintainers and contributors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import argparse
import ipaddress
import os
import sys
import tabulate

from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

from vyos.config import Config
from vyos.configquery import ConfigTreeQuery
from vyos.configdict import dict_merge
from vyos.pki import encode_certificate, encode_public_key, encode_private_key, encode_dh_parameters
from vyos.pki import create_certificate, create_certificate_request, create_certificate_revocation_list
from vyos.pki import create_private_key
from vyos.pki import create_dh_parameters
from vyos.pki import load_certificate, load_certificate_request, load_private_key, load_crl
from vyos.pki import verify_certificate
from vyos.xml import defaults
from vyos.util import ask_input, ask_yes_no
from vyos.util import cmd

CERT_REQ_END = '-----END CERTIFICATE REQUEST-----'
auth_dir = '/config/auth'

# Helper Functions
conf = ConfigTreeQuery()
def get_default_values():
    # Fetch default x509 values
    base = ['pki', 'x509', 'default']
    x509_defaults = conf.get_config_dict(base, key_mangling=('-', '_'),
                                     get_first_key=True,
                                     no_tag_node_value_mangle=True)
    default_values = defaults(base)
    x509_defaults = dict_merge(default_values, x509_defaults)

    return x509_defaults

def get_config_ca_certificate(name=None):
    # Fetch ca certificates from config
    base = ['pki', 'ca']
    if not conf.exists(base):
        return False

    if name:
        base = base + [name]
        if not conf.exists(base + ['private', 'key']) or not conf.exists(base + ['certificate']):
            return False

    return conf.get_config_dict(base, key_mangling=('-', '_'),
                                get_first_key=True,
                                no_tag_node_value_mangle=True)

def get_config_certificate(name=None):
    # Get certificates from config
    base = ['pki', 'certificate']
    if not conf.exists(base):
        return False

    if name:
        base = base + [name]
        if not conf.exists(base + ['private', 'key']) or not conf.exists(base + ['certificate']):
            return False

    return conf.get_config_dict(base, key_mangling=('-', '_'),
                                get_first_key=True,
                                no_tag_node_value_mangle=True)

def get_certificate_ca(cert, ca_certs):
    # Find CA certificate for given certificate
    for ca_name, ca_dict in ca_certs.items():
        if 'certificate' not in ca_dict:
            continue

        ca_cert = load_certificate(ca_dict['certificate'])

        if not ca_cert:
            continue

        if verify_certificate(cert, ca_cert):
            return ca_name
    return None

def get_config_revoked_certificates():
    # Fetch revoked certificates from config
    ca_base = ['pki', 'ca']
    cert_base = ['pki', 'certificate']

    certs = []

    if conf.exists(ca_base):
        ca_certificates = conf.get_config_dict(ca_base, key_mangling=('-', '_'),
                                               get_first_key=True,
                                               no_tag_node_value_mangle=True)
        certs.extend(ca_certificates.values())

    if conf.exists(cert_base):
        certificates = conf.get_config_dict(cert_base, key_mangling=('-', '_'),
                                            get_first_key=True,
                                            no_tag_node_value_mangle=True)
        certs.extend(certificates.values())

    return [cert_dict for cert_dict in certs if 'revoke' in cert_dict]

def get_revoked_by_serial_numbers(serial_numbers=[]):
    # Return serial numbers of revoked certificates
    certs_out = []
    certs = get_config_certificate()
    ca_certs = get_config_ca_certificate()
    if certs:
        for cert_name, cert_dict in certs.items():
            if 'certificate' not in cert_dict:
                continue

            cert = load_certificate(cert_dict['certificate'])
            if cert.serial_number in serial_numbers:
                certs_out.append(cert_name)
    if ca_certs:
        for cert_name, cert_dict in ca_certs.items():
            if 'certificate' not in cert_dict:
                continue

            cert = load_certificate(cert_dict['certificate'])
            if cert.serial_number in serial_numbers:
                certs_out.append(cert_name)
    return certs_out

def install_certificate(name, cert='', private_key=None, key_type=None, key_passphrase=None, is_ca=False):
    # Show conf commands for installing certificate
    prefix = 'ca' if is_ca else 'certificate'
    print('Configure mode commands to install:')

    base = f"set pki {prefix} {name}"
    if cert:
        cert_pem = "".join(encode_certificate(cert).strip().split("\n")[1:-1])
        print(f"{base} certificate '{cert_pem}'")

    if private_key:
        key_pem = "".join(encode_private_key(private_key, passphrase=key_passphrase).strip().split("\n")[1:-1])
        print(f"{base} private key '{key_pem}'")
        if key_passphrase:
            print(f"{base} private password-protected")

def install_crl(ca_name, crl):
    # Show conf commands for installing crl
    print("Configure mode commands to install CRL:")
    crl_pem = "".join(encode_certificate(crl).strip().split("\n")[1:-1])
    print(f"set pki ca {ca_name} crl '{crl_pem}'")

def install_dh_parameters(name, params):
    # Show conf commands for installing dh params
    print("Configure mode commands to install DH parameters:")
    dh_pem = "".join(encode_dh_parameters(params).strip().split("\n")[1:-1])
    print(f"set pki dh {name} parameters '{dh_pem}'")

def install_ssh_key(name, public_key, private_key, passphrase=None):
    # Show conf commands for installing ssh key
    key_openssh = encode_public_key(public_key, encoding='OpenSSH', key_format='OpenSSH')
    username = os.getlogin()
    type_key_split = key_openssh.split(" ")

    base = f"set system login user {username} authentication public-keys {name}"
    print("Configure mode commands to install SSH key:")
    print(f"{base} key '{type_key_split[1]}'")
    print(f"{base} type '{type_key_split[0]}'", end="\n\n")
    print(encode_private_key(private_key, encoding='PEM', key_format='OpenSSH', passphrase=passphrase))

def install_keypair(name, key_type, private_key=None, public_key=None, passphrase=None):
    # Show conf commands for installing key-pair
    print("Configure mode commands to install key pair:")

    if public_key:
        install_public_key = ask_yes_no('Do you want to install the public key?', default=True)
        public_key_pem = encode_public_key(public_key)

        if install_public_key:
            install_public_pem = "".join(public_key_pem.strip().split("\n")[1:-1])
            print(f"set pki key-pair {name} public key '{install_public_pem}'")
        else:
            print("Public key:")
            print(public_key_pem)

    if private_key:
        install_private_key = ask_yes_no('Do you want to install the private key?', default=True)
        private_key_pem = encode_private_key(private_key, passphrase=passphrase)

        if install_private_key:
            install_private_pem = "".join(private_key_pem.strip().split("\n")[1:-1])
            print(f"set pki key-pair {name} private key '{install_private_pem}'")
            if passphrase:
                print(f"set pki key-pair {name} private password-protected")
        else:
            print("Private key:")
            print(private_key_pem)

def install_wireguard_key(interface, private_key, public_key):
    # Show conf commands for installing wireguard key pairs
    from vyos.ifconfig import Section
    if Section.section(interface) != 'wireguard':
        print(f'"{interface}" is not a WireGuard interface name!')
        exit(1)

    # Check if we are running in a config session - if yes, we can directly write to the CLI
    cli_string = f"interfaces wireguard {interface} private-key '{private_key}'"
    if Config().in_session():
        cmd(f"/opt/vyatta/sbin/my_set {cli_string}")

        print('"generate" CLI command executed from config session.\nGenerated private-key was imported to CLI!',end='\n\n')
        print(f'Use the following command to verify: show interfaces wireguard {interface}')
    else:
        print('"generate" CLI command executed from operational level.\n'
              'Generated private-key is not stored to CLI, use configure mode commands to install key:', end='\n\n')
        print(f"set {cli_string}", end="\n\n")

    print(f"Corresponding public-key to use on peer system is: '{public_key}'")


def install_wireguard_psk(interface, peer, psk):
    from vyos.ifconfig import Section
    if Section.section(interface) != 'wireguard':
        print(f'"{interface}" is not a WireGuard interface name!')
        exit(1)

    # Check if we are running in a config session - if yes, we can directly write to the CLI
    cli_string = f"interfaces wireguard {interface} peer {peer} preshared-key '{psk}'"
    if Config().in_session():
        cmd(f"/opt/vyatta/sbin/my_set {cli_string}")

        print('"generate" CLI command executed from config session.\nGenerated preshared-key was imported to CLI!',end='\n\n')
        print(f'Use the following command to verify: show interfaces wireguard {interface}')
    else:
        print('"generate" CLI command executed from operational level.\n'
              'Generated preshared-key is not stored to CLI, use configure mode commands to install key:', end='\n\n')
        print(f"set {cli_string}", end="\n\n")


def ask_passphrase():
    passphrase = None
    print("Note: If you plan to use the generated key on this router, do not encrypt the private key.")
    if ask_yes_no('Do you want to encrypt the private key with a passphrase?'):
        passphrase = ask_input('Enter passphrase:')
    return passphrase

def write_file(filename, contents):
    full_path = os.path.join(auth_dir, filename)
    directory = os.path.dirname(full_path)

    if not os.path.exists(directory):
        print('Failed to write file: directory does not exist')
        return False

    if os.path.exists(full_path) and not ask_yes_no('Do you want to overwrite the existing file?'):
        return False

    with open(full_path, 'w') as f:
        f.write(contents)

    print(f'File written to {full_path}')

# Generation functions

def generate_private_key():
    key_type = ask_input('Enter private key type: [rsa, dsa, ec]', default='rsa', valid_responses=['rsa', 'dsa', 'ec'])

    size_valid = []
    size_default = 0

    if key_type in ['rsa', 'dsa']:
        size_default = 2048
        size_valid = [512, 1024, 2048, 4096]
    elif key_type == 'ec':
        size_default = 256
        size_valid = [224, 256, 384, 521]

    size = ask_input('Enter private key bits:', default=size_default, numeric_only=True, valid_responses=size_valid)

    return create_private_key(key_type, size), key_type

def parse_san_string(san_string):
    if not san_string:
        return None

    output = []
    san_split = san_string.strip().split(",")

    for pair_str in san_split:
        tag, value = pair_str.strip().split(":", 1)
        if tag == 'ipv4':
            output.append(ipaddress.IPv4Address(value))
        elif tag == 'ipv6':
            output.append(ipaddress.IPv6Address(value))
        elif tag == 'dns':
            output.append(value)
    return output

def generate_certificate_request(private_key=None, key_type=None, return_request=False, name=None, install=False, file=False, ask_san=True):
    if not private_key:
        private_key, key_type = generate_private_key()

    default_values = get_default_values()
    subject = {}
    subject['country'] = ask_input('Enter country code:', default=default_values['country'])
    subject['state'] = ask_input('Enter state:', default=default_values['state'])
    subject['locality'] = ask_input('Enter locality:', default=default_values['locality'])
    subject['organization'] = ask_input('Enter organization name:', default=default_values['organization'])
    subject['common_name'] = ask_input('Enter common name:', default='vyos.io')
    subject_alt_names = None

    if ask_san and ask_yes_no('Do you want to configure Subject Alternative Names?'):
        print("Enter alternative names in a comma separate list, example: ipv4:1.1.1.1,ipv6:fe80::1,dns:vyos.net")
        san_string = ask_input('Enter Subject Alternative Names:')
        subject_alt_names = parse_san_string(san_string)

    cert_req = create_certificate_request(subject, private_key, subject_alt_names)

    if return_request:
        return cert_req

    passphrase = ask_passphrase()

    if not install and not file:
        print(encode_certificate(cert_req))
        print(encode_private_key(private_key, passphrase=passphrase))
        return None

    if install:
        print("Certificate request:")
        print(encode_certificate(cert_req) + "\n")
        install_certificate(name, private_key=private_key, key_type=key_type, key_passphrase=passphrase, is_ca=False)

    if file:
        write_file(f'{name}.csr', encode_certificate(cert_req))
        write_file(f'{name}.key', encode_private_key(private_key, passphrase=passphrase))

def generate_certificate(cert_req, ca_cert, ca_private_key, is_ca=False, is_sub_ca=False):
    valid_days = ask_input('Enter how many days certificate will be valid:', default='365' if not is_ca else '1825', numeric_only=True)
    cert_type = None
    if not is_ca:
        cert_type = ask_input('Enter certificate type: (client, server)', default='server', valid_responses=['client', 'server'])
    return create_certificate(cert_req, ca_cert, ca_private_key, valid_days, cert_type, is_ca, is_sub_ca)

def generate_ca_certificate(name, install=False, file=False):
    private_key, key_type = generate_private_key()
    cert_req = generate_certificate_request(private_key, key_type, return_request=True, ask_san=False)
    cert = generate_certificate(cert_req, cert_req, private_key, is_ca=True)
    passphrase = ask_passphrase()

    if not install and not file:
        print(encode_certificate(cert))
        print(encode_private_key(private_key, passphrase=passphrase))
        return None

    if install:
        install_certificate(name, cert, private_key, key_type, key_passphrase=passphrase, is_ca=True)

    if file:
        write_file(f'{name}.pem', encode_certificate(cert))
        write_file(f'{name}.key', encode_private_key(private_key, passphrase=passphrase))

def generate_ca_certificate_sign(name, ca_name, install=False, file=False):
    ca_dict = get_config_ca_certificate(ca_name)

    if not ca_dict:
        print(f"CA certificate or private key for '{ca_name}' not found")
        return None

    ca_cert = load_certificate(ca_dict['certificate'])

    if not ca_cert:
        print("Failed to load signing CA certificate, aborting")
        return None

    ca_private = ca_dict['private']
    ca_private_passphrase = None
    if 'password_protected' in ca_private:
        ca_private_passphrase = ask_input('Enter signing CA private key passphrase:')
    ca_private_key = load_private_key(ca_private['key'], passphrase=ca_private_passphrase)

    if not ca_private_key:
        print("Failed to load signing CA private key, aborting")
        return None

    private_key = None
    key_type = None

    cert_req = None
    if not ask_yes_no('Do you already have a certificate request?'):
        private_key, key_type = generate_private_key()
        cert_req = generate_certificate_request(private_key, key_type, return_request=True, ask_san=False)
    else:
        print("Paste certificate request and press enter:")
        lines = []
        curr_line = ''
        while True:
            curr_line = input().strip()
            if not curr_line or curr_line == CERT_REQ_END:
                break
            lines.append(curr_line)

        if not lines:
            print("Aborted")
            return None

        wrap = lines[0].find('-----') < 0 # Only base64 pasted, add the CSR tags for parsing
        cert_req = load_certificate_request("\n".join(lines), wrap)

    if not cert_req:
        print("Invalid certificate request")
        return None

    cert = generate_certificate(cert_req, ca_cert, ca_private_key, is_ca=True, is_sub_ca=True)
    passphrase = ask_passphrase()

    if not install and not file:
        print(encode_certificate(cert))
        print(encode_private_key(private_key, passphrase=passphrase))
        return None

    if install:
        install_certificate(name, cert, private_key, key_type, key_passphrase=passphrase, is_ca=True)

    if file:
        write_file(f'{name}.pem', encode_certificate(cert))
        write_file(f'{name}.key', encode_private_key(private_key, passphrase=passphrase))

def generate_certificate_sign(name, ca_name, install=False, file=False):
    ca_dict = get_config_ca_certificate(ca_name)

    if not ca_dict:
        print(f"CA certificate or private key for '{ca_name}' not found")
        return None

    ca_cert = load_certificate(ca_dict['certificate'])

    if not ca_cert:
        print("Failed to load CA certificate, aborting")
        return None

    ca_private = ca_dict['private']
    ca_private_passphrase = None
    if 'password_protected' in ca_private:
        ca_private_passphrase = ask_input('Enter CA private key passphrase:')
    ca_private_key = load_private_key(ca_private['key'], passphrase=ca_private_passphrase)

    if not ca_private_key:
        print("Failed to load CA private key, aborting")
        return None

    private_key = None
    key_type = None

    cert_req = None
    if not ask_yes_no('Do you already have a certificate request?'):
        private_key, key_type = generate_private_key()
        cert_req = generate_certificate_request(private_key, key_type, return_request=True)
    else:
        print("Paste certificate request and press enter:")
        lines = []
        curr_line = ''
        while True:
            curr_line = input().strip()
            if not curr_line or curr_line == CERT_REQ_END:
                break
            lines.append(curr_line)

        if not lines:
            print("Aborted")
            return None

        wrap = lines[0].find('-----') < 0 # Only base64 pasted, add the CSR tags for parsing
        cert_req = load_certificate_request("\n".join(lines), wrap)

    if not cert_req:
        print("Invalid certificate request")
        return None

    cert = generate_certificate(cert_req, ca_cert, ca_private_key, is_ca=False)
    passphrase = ask_passphrase()

    if not install and not file:
        print(encode_certificate(cert))
        print(encode_private_key(private_key, passphrase=passphrase))
        return None

    if install:
        install_certificate(name, cert, private_key, key_type, key_passphrase=passphrase, is_ca=False)

    if file:
        write_file(f'{name}.pem', encode_certificate(cert))
        write_file(f'{name}.key', encode_private_key(private_key, passphrase=passphrase))

def generate_certificate_selfsign(name, install=False, file=False):
    private_key, key_type = generate_private_key()
    cert_req = generate_certificate_request(private_key, key_type, return_request=True)
    cert = generate_certificate(cert_req, cert_req, private_key, is_ca=False)
    passphrase = ask_passphrase()

    if not install and not file:
        print(encode_certificate(cert))
        print(encode_private_key(private_key, passphrase=passphrase))
        return None

    if install:
        install_certificate(name, cert, private_key=private_key, key_type=key_type, key_passphrase=passphrase, is_ca=False)

    if file:
        write_file(f'{name}.pem', encode_certificate(cert))
        write_file(f'{name}.key', encode_private_key(private_key, passphrase=passphrase))

def generate_certificate_revocation_list(ca_name, install=False, file=False):
    ca_dict = get_config_ca_certificate(ca_name)

    if not ca_dict:
        print(f"CA certificate or private key for '{ca_name}' not found")
        return None

    ca_cert = load_certificate(ca_dict['certificate'])

    if not ca_cert:
        print("Failed to load CA certificate, aborting")
        return None

    ca_private = ca_dict['private']
    ca_private_passphrase = None
    if 'password_protected' in ca_private:
        ca_private_passphrase = ask_input('Enter CA private key passphrase:')
    ca_private_key = load_private_key(ca_private['key'], passphrase=ca_private_passphrase)

    if not ca_private_key:
        print("Failed to load CA private key, aborting")
        return None

    revoked_certs = get_config_revoked_certificates()
    to_revoke = []

    for cert_dict in revoked_certs:
        if 'certificate' not in cert_dict:
            continue

        cert_data = cert_dict['certificate']

        try:
            cert = load_certificate(cert_data)

            if cert.issuer == ca_cert.subject:
                to_revoke.append(cert.serial_number)
        except ValueError:
            continue

    if not to_revoke:
        print("No revoked certificates to add to the CRL")
        return None

    crl = create_certificate_revocation_list(ca_cert, ca_private_key, to_revoke)

    if not crl:
        print("Failed to create CRL")
        return None

    if not install and not file:
        print(encode_certificate(crl))
        return None

    if install:
        install_crl(ca_name, crl)

    if file:
        write_file(f'{name}.crl', encode_certificate(crl))

def generate_ssh_keypair(name, install=False, file=False):
    private_key, key_type = generate_private_key()
    public_key = private_key.public_key()
    passphrase = ask_passphrase()

    if not install and not file:
        print(encode_public_key(public_key, encoding='OpenSSH', key_format='OpenSSH'))
        print("")
        print(encode_private_key(private_key, encoding='PEM', key_format='OpenSSH', passphrase=passphrase))
        return None

    if install:
        install_ssh_key(name, public_key, private_key, passphrase)

    if file:
        write_file(f'{name}.pem', encode_public_key(public_key, encoding='OpenSSH', key_format='OpenSSH'))
        write_file(f'{name}.key', encode_private_key(private_key, encoding='PEM', key_format='OpenSSH', passphrase=passphrase))

def generate_dh_parameters(name, install=False, file=False):
    bits = ask_input('Enter DH parameters key size:', default=2048, numeric_only=True)

    print("Generating parameters...")

    dh_params = create_dh_parameters(bits)
    if not dh_params:
        print("Failed to create DH parameters")
        return None

    if not install and not file:
        print("DH Parameters:")
        print(encode_dh_parameters(dh_params))

    if install:
        install_dh_parameters(name, dh_params)

    if file:
        write_file(f'{name}.pem', encode_dh_parameters(dh_params))

def generate_keypair(name, install=False, file=False):
    private_key, key_type = generate_private_key()
    public_key = private_key.public_key()
    passphrase = ask_passphrase()

    if not install and not file:
        print(encode_public_key(public_key))
        print("")
        print(encode_private_key(private_key, passphrase=passphrase))
        return None

    if install:
        install_keypair(name, key_type, private_key, public_key, passphrase)

    if file:
        write_file(f'{name}.pem', encode_public_key(public_key))
        write_file(f'{name}.key', encode_private_key(private_key, passphrase=passphrase))

def generate_openvpn_key(name, install=False, file=False):
    result = cmd('openvpn --genkey secret /dev/stdout | grep -o "^[^#]*"')

    if not result:
        print("Failed to generate OpenVPN key")
        return None

    if not install and not file:
        print(result)
        return None

    if install:
        key_lines = result.split("\n")
        key_data = "".join(key_lines[1:-1]) # Remove wrapper tags and line endings
        key_version = '1'

        import re
        version_search = re.search(r'BEGIN OpenVPN Static key V(\d+)', result) # Future-proofing (hopefully)
        if version_search:
            key_version = version_search[1]

        base = f"set pki openvpn shared-secret {name}"
        print("Configure mode commands to install OpenVPN key:")
        print(f"{base} key '{key_data}'")
        print(f"{base} version '{key_version}'")

    if file:
        write_file(f'{name}.key', result)

def generate_wireguard_key(interface=None, install=False):
    private_key = cmd('wg genkey')
    public_key = cmd('wg pubkey', input=private_key)

    if interface and install:
        install_wireguard_key(interface, private_key, public_key)
    else:
        print(f'Private key: {private_key}')
        print(f'Public key: {public_key}', end='\n\n')

def generate_wireguard_psk(interface=None, peer=None, install=False):
    psk = cmd('wg genpsk')
    if interface and peer and install:
        install_wireguard_psk(interface, peer, psk)
    else:
        print(f'Pre-shared key: {psk}')

# Show functions
def show_certificate_authority(name=None):
    headers = ['Name', 'Subject', 'Issuer CN', 'Issued', 'Expiry', 'Private Key', 'Parent']
    data = []
    certs = get_config_ca_certificate()
    if certs:
        for cert_name, cert_dict in certs.items():
            if name and name != cert_name:
                continue
            if 'certificate' not in cert_dict:
                continue

            cert = load_certificate(cert_dict['certificate'])
            parent_ca_name = get_certificate_ca(cert, certs)
            cert_issuer_cn = cert.issuer.rfc4514_string().split(",")[0]

            if not parent_ca_name or parent_ca_name == cert_name:
                parent_ca_name = 'N/A'

            if not cert:
                continue

            have_private = 'Yes' if 'private' in cert_dict and 'key' in cert_dict['private'] else 'No'
            data.append([cert_name, cert.subject.rfc4514_string(), cert_issuer_cn, cert.not_valid_before, cert.not_valid_after, have_private, parent_ca_name])

    print("Certificate Authorities:")
    print(tabulate.tabulate(data, headers))

def show_certificate(name=None):
    headers = ['Name', 'Type', 'Subject CN', 'Issuer CN', 'Issued', 'Expiry', 'Revoked', 'Private Key', 'CA Present']
    data = []
    certs = get_config_certificate()
    if certs:
        ca_certs = get_config_ca_certificate()

        for cert_name, cert_dict in certs.items():
            if name and name != cert_name:
                continue
            if 'certificate' not in cert_dict:
                continue

            cert = load_certificate(cert_dict['certificate'])

            if not cert:
                continue

            ca_name = get_certificate_ca(cert, ca_certs)
            cert_subject_cn = cert.subject.rfc4514_string().split(",")[0]
            cert_issuer_cn = cert.issuer.rfc4514_string().split(",")[0]
            cert_type = 'Unknown'
            ext = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
            if ext and ExtendedKeyUsageOID.SERVER_AUTH in ext.value:
                cert_type = 'Server'
            elif ext and ExtendedKeyUsageOID.CLIENT_AUTH in ext.value:
                cert_type = 'Client'

            revoked = 'Yes' if 'revoke' in cert_dict else 'No'
            have_private = 'Yes' if 'private' in cert_dict and 'key' in cert_dict['private'] else 'No'
            have_ca = f'Yes ({ca_name})' if ca_name else 'No'
            data.append([
                cert_name, cert_type, cert_subject_cn, cert_issuer_cn,
                cert.not_valid_before, cert.not_valid_after,
                revoked, have_private, have_ca])

    print("Certificates:")
    print(tabulate.tabulate(data, headers))

def show_crl(name=None):
    headers = ['CA Name', 'Updated', 'Revokes']
    data = []
    certs = get_config_ca_certificate()
    if certs:
        for cert_name, cert_dict in certs.items():
            if name and name != cert_name:
                continue
            if 'crl' not in cert_dict:
                continue

            crls = cert_dict['crl']
            if isinstance(crls, str):
                crls = [crls]

            for crl_data in cert_dict['crl']:
                crl = load_crl(crl_data)

                if not crl:
                    continue

                certs = get_revoked_by_serial_numbers([revoked.serial_number for revoked in crl])
                data.append([cert_name, crl.last_update, ", ".join(certs)])

    print("Certificate Revocation Lists:")
    print(tabulate.tabulate(data, headers))

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--action', help='PKI action', required=True)

    # X509
    parser.add_argument('--ca', help='Certificate Authority', required=False)
    parser.add_argument('--certificate', help='Certificate', required=False)
    parser.add_argument('--crl', help='Certificate Revocation List', required=False)
    parser.add_argument('--sign', help='Sign certificate with specified CA', required=False)
    parser.add_argument('--self-sign', help='Self-sign the certificate', action='store_true')

    # SSH
    parser.add_argument('--ssh', help='SSH Key', required=False)

    # DH
    parser.add_argument('--dh', help='DH Parameters', required=False)

    # Key pair
    parser.add_argument('--keypair', help='Key pair', required=False)

    # OpenVPN
    parser.add_argument('--openvpn', help='OpenVPN TLS key', required=False)

    # WireGuard
    parser.add_argument('--wireguard', help='Wireguard', action='store_true')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--key', help='Wireguard key pair', action='store_true', required=False)
    group.add_argument('--psk', help='Wireguard pre shared key', action='store_true', required=False)
    parser.add_argument('--interface', help='Install generated keys into running-config for named interface', action='store')
    parser.add_argument('--peer', help='Install generated keys into running-config for peer', action='store')

    # Global
    parser.add_argument('--file', help='Write generated keys into specified filename', action='store_true')
    parser.add_argument('--install', help='Install generated keys into running-config', action='store_true')

    args = parser.parse_args()

    try:
        if args.action == 'generate':
            if args.ca:
                if args.sign:
                    generate_ca_certificate_sign(args.ca, args.sign, install=args.install, file=args.file)
                else:
                    generate_ca_certificate(args.ca, install=args.install, file=args.file)
            elif args.certificate:
                if args.sign:
                    generate_certificate_sign(args.certificate, args.sign, install=args.install, file=args.file)
                elif args.self_sign:
                    generate_certificate_selfsign(args.certificate, install=args.install, file=args.file)
                else:
                    generate_certificate_request(name=args.certificate, install=args.install, file=args.file)

            elif args.crl:
                generate_certificate_revocation_list(args.crl, install=args.install, file=args.file)

            elif args.ssh:
                generate_ssh_keypair(args.ssh, install=args.install, file=args.file)

            elif args.dh:
                generate_dh_parameters(args.dh, install=args.install, file=args.file)

            elif args.keypair:
                generate_keypair(args.keypair, install=args.install, file=args.file)

            elif args.openvpn:
                generate_openvpn_key(args.openvpn, install=args.install, file=args.file)

            elif args.wireguard:
                # WireGuard supports writing key directly into the CLI, but this
                # requires the vyos_libexec_dir environment variable to be set
                os.environ["vyos_libexec_dir"] = "/usr/libexec/vyos"

                if args.key:
                    generate_wireguard_key(args.interface, install=args.install)
                if args.psk:
                    generate_wireguard_psk(args.interface, peer=args.peer, install=args.install)

        elif args.action == 'show':
            if args.ca:
                show_certificate_authority(None if args.ca == 'all' else args.ca)
            elif args.certificate:
                show_certificate(None if args.certificate == 'all' else args.certificate)
            elif args.crl:
                show_crl(None if args.crl == 'all' else args.crl)
            else:
                show_certificate_authority()
                show_certificate()
                show_crl()
    except KeyboardInterrupt:
        print("Aborted")
        sys.exit(0)
