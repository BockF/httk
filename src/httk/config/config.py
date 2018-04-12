# 
#    The high-throughput toolkit (httk)
#    Copyright (C) 2012-2015 Rickard Armiento
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
Read and setup httk configuration and versioning data. 

httk_python_root is derived as the directory config.py is in + '..'

config is a configparser.config object where
  - All assignments in a distdata.py file in httk_python_root are read into the section [general]
  - Read httk.cfg in httk_python_root
  - Using the latest definition of [general]/httk_root, read httk.cfg in that directory
  - Read ~/.httk.cfg

In this config object, the section [general] is looked up for 'httk_root', which is exported as httk_root. If not present, 'root' is looked up in 
the section 'distdata'. If that is not present, the default of httk_python_root + ../.. is used.

if the file distdata.py in httk_python_root exists, the config object section [distdata] is looked up for version, version_date, and copyright_note, 
which are exported as httk_version, httk_version_date, httk_copyright_note. If this file does not exist, they identifiers are instead derived using the 'git' command. 
If that does not work, they are set to 'unknown', except for httk_copyright_note, which is set to a sensible default.

This python file has no dependencies except for the standard library (neither within httk or outside).
It will always remain safe to import by itself, e.g.::

  (cd src/httk/config; python -c "import sys, config; sys.stdout.write(config.httk_version + '\n')")

Or::

python -c "import sys; here = path.abspath(path.dirname(__file__)); sys.path.insert(1, os.path.join(here,'src/httk/config')); import config; sys.stdout.write(config.httk_version + '\n')
"""

_default_httk_root = '../..'
_default_copyright_note = "(c) 2012 - 2018, Rickard Armiento, et al."

import sys, os, inspect, subprocess, datetime 
try:
    from StringIO import StringIO
except ImportError:
    from io import StringIO

try:
    # Python 2
    import ConfigParser as configparser
except ImportError:
    # Python 3
    import configparser

httk_python_root = os.path.realpath(os.path.join(os.path.abspath(os.path.split(inspect.getfile(inspect.currentframe()))[0]),'..')) 
httk_root = None
config = configparser.ConfigParser()

def read_config():
    global httk_python_root, httk_root, config

    try:
        with open(os.path.join(httk_python_root, "distdata.py"), 'r') as fp:
            distdata_str = fp.read()
            ini_str = '[distdata]\n' + distdata_str
            ini_fp = StringIO(ini_str)
            config.readfp(ini_fp)
            httk_root = os.path.realpath(os.path.join(httk_python_root,config.get('distdata','root').strip('"')))
    except (IOError, configparser.NoSectionError, configparser.NoOptionError):
        httk_root = os.path.realpath(os.path.join(httk_python_root,_default_httk_root))
    
    config_files = []
    internal_cfgpathstr = os.path.join(httk_python_root, 'httk.cfg')
    config.read([internal_cfgpathstr])
   
    try:
        httk_root_cfg = config.get('general','httk_root')
        httk_root = os.path.join(httk_python_root,httk_root_cfg)
    except (configparser.NoSectionError, configparser.NoOptionError):
        pass
    
    global_cfgpathstr = os.path.join(httk_python_root, 'httk.cfg')
    local_cfgpathstr = os.path.expanduser('~/.httk.cfg')

    try:
        httk_root_cfg = config.get('general','httk_root')
        httk_root = os.path.join(httk_python_root,httk_root_cfg)
    except (configparser.NoSectionError, configparser.NoOptionError):
        pass

    config.read([global_cfgpathstr, local_cfgpathstr])

def determine_version_data():
    global httk_python_root, httk_root, config

    httk_version = None
    if os.path.exists(os.path.join(httk_python_root, "distdata.py")):
        try:
            httk_version = config.get('distdata','version').strip('"')
            httk_version_date = config.get('distdata','version_date').strip('"')
            httk_copyright_note = config.get('distdata','copyright_note').strip('"')
        except (configparser.NoSectionError, configparser.NoOptionError):
            pass

    if httk_version is None:
        if (not config.getboolean('general', 'bypass_git_version_lookup')) and os.path.exists(os.path.join(httk_root,'.git')):
            try:
                httk_version = subprocess.check_output(["git", "describe","--dirty","--always"]).strip()
                if httk_version.endswith('-dirty'):
                    _git_commit_datetime = datetime.datetime.now()
                else:
                    _git_commit_datetime = datetime.datetime.fromtimestamp(int(subprocess.check_output(["git", "log","-1",'--format=%ct'])))
                httk_version_date = "%d-%02d-%02d" % (_git_commit_datetime.year,_git_commit_datetime.month, _git_commit_datetime.day)
                httk_copyright_note = "(c) 2012 - " + str(_git_commit_datetime.year)

                # PEP 440 compliance
                httk_version = httk_version[1:]
                httk_version = httk_version.replace('-','.dev',1)
                httk_version = httk_version.replace('-','+',1)
                if httk_version.endswith('-dirty'):
                    httk_version = httk_version.replace('-dirty','.d')

            except Exception as e:
                sys.stderr.write("Note: failed to obtain httk version from git: " + str(e) + "\n")
                httk_version = 'unknown'
                httk_version_date = 'unknown'
                httk_copyright_note = _default_copyright_note

        else:
            httk_version = 'unknown'
            httk_version_date = 'unknown'
            httk_copyright_note = _default_copyright_note

    return {'httk_version':httk_version, 'httk_version_date':httk_version_date, 'httk_copyright_note':httk_copyright_note}

read_config()
_version_data = determine_version_data()
httk_version = _version_data['httk_version']
httk_version_date = _version_data['httk_version_date']
httk_copyright_note = _version_data['httk_copyright_note']
