# -*- coding: utf-8 -*-
import argparse
import atexit
import datetime
import inspect
import json
import logging
import os
import platform
import re
import urllib2
import shutil
import socket
import subprocess
import sys
import time

try:
    import selenium
    from selenium import webdriver
    from selenium.common.exceptions import NoSuchElementException
    from selenium.common.exceptions import TimeoutException
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.select import Select
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:
    print('Please install package selenium')
    exit(1)

class Diag(object):
    def __init__(self):
        # argument
        parser = argparse.ArgumentParser(description='Chrome Diagnostic Tool', formatter_class=argparse.ArgumentDefaultsHelpFormatter, epilog='''
    examples:
      python %(prog)s
    ''')
        parser.add_argument('--browser-name', dest='browser_name', help='name of browser', default='chrome_canary')
        parser.add_argument('--browser-options', dest='browser_options', help='extra options of browser, split by ","')
        parser.add_argument('--browser-path', dest='browser_path', help='path of browser')
        parser.add_argument('--webdriver-path', dest='webdriver_path', help='path of webdriver')
        parser.add_argument('--os-name', dest='os_name', help='OS to run test on')
        parser.add_argument('--android-device-id', dest='android_device_id', help='id of Android device to run test on')
        parser.add_argument('--logging-level', dest='logging_level', help='level of logging', default=logging.INFO)

        debug_group = parser.add_argument_group('debug')
        debug_group.add_argument('--fixed-time', dest='fixed_time', help='fixed time', action='store_true')
        args = parser.parse_args()

        # timestamp
        if args.fixed_time:
            self.timestamp = Util.get_datetime(format='%Y%m%d')
        else:
            self.timestamp = Util.get_datetime()

        # log
        work_dir = Util.use_slash(sys.path[0])
        os.chdir(work_dir)
        self.log_dir = 'ignore/log'
        Util.ensure_dir(self.log_dir)
        self.log_file = '%s/%s.log' % (self.log_dir, self.timestamp)
        Util.ensure_nofile(self.log_file)
        Util.set_logger(self.log_file, args.logging_level)
        self._logger = Util.get_logger()

        # device
        if args.os_name == 'android':
            self.android_device = AndroidDevices().get_device(args.android_device_id)
        else:
            self.android_device = None

        # OS
        self.host_os = HostOS()
        if args.os_name == 'android':
            self.target_os = AndroidOS(self.android_device)
        else:
            self.target_os = self.host_os

        # browser
        if args.browser_name:
            browser_name = args.browser_name
        elif self.target_os.is_cros():
            browser_name = 'chrome'
        else:
            Util.error('Please designate browser name')

        if args.browser_options:
            browser_options = args.browser_options.split(',')
        else:
            browser_options = []

        if 'chrome' in browser_name and not self.target_os.is_android() and not self.target_os.is_cros():
            user_data_dir = 'ignore/user-data-dir-%s' % self.target_os.username
            browser_options.append('--user-data-dir=%s' % (work_dir + '/' + user_data_dir))
            Util.ensure_nodir(user_data_dir)
            Util.ensure_dir(user_data_dir)

        self.browser = Browser(name=browser_name, path=args.browser_path, options=browser_options, os=self.target_os)

        self.webdriver_path = args.webdriver_path
        self.webdriver = Webdriver(browser=self.browser, path=self.webdriver_path, host_os=self.host_os, target_os=self.target_os, android_device=self.android_device)
        self.driver = self.webdriver.driver

        # GPU
        self.gpus = GPUs(self.target_os, self.android_device, self.driver)
        self.gpu = self.gpus.get_active(self.driver)

        self.driver.get('chrome://gpu')
        try:
            WebDriverWait(self.driver, 60).until(lambda driver: driver.find_element_by_id('basic-info').find_elements_by_xpath('./div/table/tbody/tr'))
        except TimeoutException:
            Util.error('Could not get GPU info')
        time.sleep(2)

        fo = open('ignore/gpu.html', "w")
        fo.write(self.driver.page_source)
        fo.close()
        time.sleep(2)


        trs = self.driver.find_element_by_id('basic-info').find_elements_by_xpath('./div/table/tbody/tr')
        for tr in trs:
            tds = tr.find_elements_by_xpath('./td')
            key = tds[0].find_element_by_xpath('./span').text
            if key == 'GL_RENDERER':
                product_name = tds[1].find_element_by_xpath('./span').text
                break
        print product_name

class Cmd(object):
    def __init__(self, cmd, show_cmd=False, dryrun=False, abort=False):
        self._logger = Util.get_logger()
        self.cmd = cmd
        self.show_cmd = show_cmd
        self.dryrun = dryrun
        self.abort = abort

        if self.show_cmd:
            self._logger.info('[CMD]: %s' % self.cmd)

        if self.dryrun:
            self.status = 0
            self.output = ''
            self.process = None
            return

        tmp_output = ''
        process = subprocess.Popen(self.cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        while True:
            nextline = process.stdout.readline()
            if nextline == '' and process.poll() is not None:
                break
            tmp_output += nextline

        self.status = process.returncode
        (out, error) = process.communicate()
        self.output = tmp_output + out + error
        self.process = process

        if self.abort and self.status:
            Util.error('Failed to execute %s' % cmd, error_code=self.status)

class Util(object):
    LOGGER_NAME = __file__

    @staticmethod
    def diff_list(a, b):
        return list(set(a).difference(set(b)))

    @staticmethod
    def intersect_list(a, b):
        return list(set(a).intersection(set(b)))

    @staticmethod
    def ensure_dir(dir_path):
        if not os.path.exists(dir_path):
            os.makedirs(dir_path)

    @staticmethod
    def ensure_nodir(dir_path):
        if os.path.exists(dir_path):
            shutil.rmtree(dir_path)

    @staticmethod
    def ensure_file(file_path):
        Util.ensure_dir(os.path.dirname(os.path.abspath(file_path)))
        if not os.path.exists(file_path):
            Cmd('touch ' + file_path)

    @staticmethod
    def ensure_nofile(file_path):
        if os.path.exists(file_path):
            os.remove(file_path)

    @staticmethod
    def error(msg):
        _logger = Util.get_logger()
        _logger.error(msg)
        exit(1)

    @staticmethod
    def not_implemented():
        Util.error('not_mplemented() at line %s' % inspect.stack()[1][2])

    @staticmethod
    def get_caller_name():
        return inspect.stack()[1][3]

    @staticmethod
    def get_datetime(format='%Y%m%d%H%M%S'):
        return time.strftime(format, time.localtime())

    @staticmethod
    def get_env(env):
        return os.getenv(env)

    @staticmethod
    def set_env(env, value):
        if value:
            os.environ[env] = value

    @staticmethod
    def unset_env(env):
        if env in os.environ:
            del os.environ[env]

    @staticmethod
    def get_executable_suffix(host_os):
        if host_os.is_win():
            return '.exe'
        else:
            return ''

    @staticmethod
    def get_logger():
        return logging.getLogger(Util.LOGGER_NAME)

    @staticmethod
    def set_logger(log_file, level, show_time=False):
        if show_time:
            formatter = logging.Formatter('[%(asctime)s - %(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S')
        else:
            formatter = logging.Formatter('[%(levelname)s] %(message)s')
        logger = logging.getLogger(Util.LOGGER_NAME)
        logger.setLevel(level)

        log_file = logging.FileHandler(log_file)
        log_file.setFormatter(formatter)
        logger.addHandler(log_file)

        console = logging.StreamHandler()
        console.setFormatter(formatter)
        logger.addHandler(console)

    @staticmethod
    def has_pkg(pkg):
        cmd = Cmd('dpkg -s ' + pkg)
        if cmd.status:
            return False
        else:
            return True

    @staticmethod
    def read_file(file_path):
        if not os.path.exists(file_path):
            return []

        f = open(file_path)
        lines = [line.rstrip('\n') for line in f]
        if len(lines) > 0:
            while (lines[-1] == ''):
                del lines[-1]
        f.close()
        return lines

    @staticmethod
    def use_slash(s):
        if s:
            return s.replace('\\', '/')
        else:
            return s

    @staticmethod
    def SendInput(*inputs):
        nInputs = len(inputs)
        LPINPUT = INPUT * nInputs
        pInputs = LPINPUT(*inputs)
        cbSize = ctypes.c_int(ctypes.sizeof(INPUT))
        return ctypes.windll.user32.SendInput(nInputs, pInputs, cbSize)

class GPU(object):
    VENDOR_NAME_ID = {
        'amd': '1002',
        'intel': '8086',
        'nvidia': '10DE',
        'qualcomm': '5143',
    }
    VENDOR_NAMES = VENDOR_NAME_ID.keys()

    # produce info is from https://en.wikipedia.org/wiki/List_of_Intel_graphics_processing_units
    INTEL_GEN_ID = {
        '6': '0102,0106,0112,0116,0122,0126,010A',
        '7': '0152,0156,015A,0162,0166,016A',
        '7.5': '0402,0406,040A,040B,040E,0A02,0A06,0A0A,0A0B,0A0E,0C02,0C06,0C0A,0C0B,0C0E,0D02,0D06,0D0A,0D0B,0D0E' +
               '0412,0416,041A,041B,041E,0A12,0A16,0A1A,0A1B,0A1E,0C12,0C16,0C1A,0C1B,0C1E,0D12,0D16,0D1A,0D1B,0D1E' +
               '0422,0426,042A,042B,042E,0A22,0A26,0A2A,0A2B,0A2E,0C22,0C26,0C2A,0C2B,0C2E,0D22,0D26,0D2A,0D2B,0D2E',
        '8': '1606,161E,1616,1612,1626,162B,1622,22B0,22B1,22B2,22B3',
        '9': '1906,1902,191E,1916,191B,1912,191D,1926,193B,193D,0A84,1A84,1A85,5A84,5A85',
        '9.5': '5912',
    }

    def __init__(self, vendor_name, vendor_id, product_name, product_id, driver_version):
        self.vendor_name = vendor_name.lower()
        self.vendor_id = vendor_id
        # We may not get vendor_name and vendor_id at the same time. For example, only vendor_name is available on Android.
        for vendor_name in self.VENDOR_NAME_ID:
            if self._is_vendor_name(vendor_name):
                if not self.vendor_name:
                    self.vendor_name = vendor_name
                if not self.vendor_id:
                    self.vendor_id = self.VENDOR_NAME_ID[vendor_name]

        self.product_name = product_name
        self.product_id = product_id
        self.driver_version = driver_version

        # intel_gen
        if self.is_intel():
            for gen in self.INTEL_GEN_ID:
                if self.product_id in self.INTEL_GEN_ID[gen]:
                    self.intel_gen = gen
                    break
            else:
                self.intel_gen = ''
        else:
            self.intel_gen = ''

    def is_amd(self):
        return self._is_vendor_name('amd')

    def is_intel(self):
        return self._is_vendor_name('intel')

    def is_nvidia(self):
        return self._is_vendor_name('nvidia')

    def is_qualcomm(self):
        return self._is_vendor_name('qualcomm')

    def _is_vendor_name(self, vendor_name):
        return self.vendor_name == vendor_name or self.vendor_id == self.VENDOR_NAME_ID[vendor_name]

    def __str__(self):
        return json.dumps({
            'vendor_name': self.vendor_name,
            'vendor_id': self.vendor_id,
            'product_name': self.product_name,
            'product_id': self.product_id,
            'intel_gen': self.intel_gen
        })


class GPUs(object):
    def __init__(self, os, android_device, driver=None):
        self._logger = Util.get_logger()
        self.gpus = []

        vendor_name = []
        vendor_id = []
        product_name = []
        product_id = []
        driver_version = []

        if os.is_android():
            cmd = AdbShellCmd('dumpsys | grep GLES', android_device.id)
            for line in cmd.output.split('\n'):
                if re.match('GLES', line):
                    fields = line.replace('GLES:', '').strip().split(',')
                    vendor_name.append(fields[0])
                    vendor_id.append('')
                    product_name.append(fields[1])
                    product_id.append('')
                    driver_version.append('')
                    break

        elif os.is_cros():
            driver.get('chrome://gpu')
            try:
                WebDriverWait(driver, 60).until(lambda driver: driver.find_element_by_id('basic-info'))
            except TimeoutException:
                Util.error('Could not get GPU info')

            trs = driver.find_element_by_id('basic-info').find_elements_by_xpath('./div/table/tbody/tr')
            for tr in trs:
                tds = tr.find_elements_by_xpath('./td')
                key = tds[0].find_element_by_xpath('./span').text
                if key == 'GPU0':
                    value = tds[1].find_element_by_xpath('./span').text
                    match = re.search('VENDOR = 0x(\S{4}), DEVICE.*= 0x(\S{4})', value)
                    vendor_id.append(match.group(1))
                    vendor_name.append('')
                    product_id.append(match.group(2))
                if key == 'Driver version':
                    driver_version.append(tds[1].find_element_by_xpath('./span').text)
                if key == 'GL_RENDERER':
                    product_name.append(tds[1].find_element_by_xpath('./span').text)
                    break

        elif os.is_linux():
            cmd = Cmd('lshw -numeric -c display')
            lines = cmd.output.split('\n')
            for line in lines:
                line = line.strip()
                match = re.search('product: (.*) \[(.*)\]$', line)
                if match:
                    product_name.append(match.group(1))
                    product_id.append(match.group(2).split(':')[1].upper())
                match = re.search('vendor: (.*) \[(.*)\]$', line)
                if match:
                    vendor_name.append(match.group(1))
                    vendor_id.append(match.group(2).upper())
                    driver_version.append('')
                    break

        elif os.is_mac():
            cmd = Cmd('system_profiler SPDisplaysDataType')
            lines = cmd.output.split('\n')
            for line in lines:
                line = line.strip()
                match = re.match('Chipset Model: (.*)', line)
                if match:
                    product_name.append(match.group(1))
                match = re.match('Vendor: (.*) \(0x(.*)\)', line)
                if match:
                    vendor_name.append(match.group(1))
                    vendor_id.append(match.group(2))
                match = re.match('Device ID: 0x(.*)', line)
                if match:
                    product_id.append(match.group(1))
                    driver_version.append('')

        elif os.is_win():
            cmd = Cmd('wmic path win32_videocontroller get /format:list')
            lines = cmd.output.split('\n')
            for line in lines:
                line = line.rstrip('\r')
                match = re.match('AdapterCompatibility=(.*)', line)
                if match:
                    vendor_name.append(match.group(1))
                match = re.match('DriverVersion=(.*)', line)
                if match:
                    driver_version.append(match.group(1))
                match = re.match('Name=(.*)', line)
                if match:
                    product_name.append(match.group(1))
                match = re.match('PNPDeviceID=.*VEN_(\S{4})&.*DEV_(\S{4})&', line)
                if match:
                    vendor_id.append(match.group(1))
                    product_id.append(match.group(2))

        for index in range(len(vendor_name)):
            self.gpus.append(GPU(vendor_name[index], vendor_id[index], product_name[index], product_id[index], driver_version[index]))

        if len(self.gpus) < 1:
            Util.error('Could not find any GPU')

    def get_active(self, driver):
        if not driver or len(self.gpus) == 1:
            return self.gpus[0]
        else:
            try:
                debug_info = driver.execute_script('''
                    var canvas = document.createElement("canvas");
                    var gl = canvas.getContext("webgl");
                    var ext = gl.getExtension("WEBGL_debug_renderer_info");
                    return gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) + " " + gl.getParameter(ext.UNMASKED_RENDERER_WEBGL);
                ''')
            except WebDriverException:
                self._logger.warning('WEBGL_debug_renderer_info is not supported, so we assume first GPU from %s will be used' % self.gpus[0].vendor_name)
            else:
                for gpu in self.gpus:
                    if re.search(gpu.vendor_name, debug_info, re.I) or re.search(gpu.product_name, debug_info, re.I):
                        return gpu
                else:
                    self._logger.warning('Could not find the active GPU, so we assume first GPU from %s will be used' % self.gpus[0].vendor_name)

class OS(object):
    def __init__(self, name, version=''):
        self.name = name
        self.version = version

    def is_android(self):
        return self._is_name('android')

    def is_cros(self):
        return self._is_name('cros')

    def is_linux(self):
        return self._is_name('linux')

    def is_mac(self):
        return self._is_name('mac')

    def is_win(self):
        return self._is_name('win')

    def _is_name(self, name):
        return self.name == name

    def __str__(self):
        return json.dumps({
            'name': self.name,
            'version': self.version,
        })

class HostOS(OS):
    def __init__(self):
        # name
        system = platform.system().lower()
        if system == 'linux':
            cmd = Cmd('cat /etc/lsb-release')
            if re.search('CHROMEOS', cmd.output, re.I):
                self.name = 'cros'
            else:
                self.name = 'linux'
        elif system == 'darwin':
            self.name = 'mac'
        elif system == 'windows':
            self.name = 'win'

        # version
        if self.is_cros():
            version = platform.platform()
        elif self.is_linux():
            version = platform.dist()[1]
        elif self.is_mac():
            version = platform.mac_ver()[0]
        elif self.is_win():
            version = platform.version()

        super(HostOS, self).__init__(self.name, version)

        # host_os specific variables
        if self.is_win():
            self.appdata = Util.use_slash(Util.get_env('APPDATA'))
            self.programfiles = Util.use_slash(Util.get_env('PROGRAMFILES'))
            self.programfilesx86 = Util.use_slash(Util.get_env('PROGRAMFILES(X86)'))
            self.windir = Util.use_slash(Util.get_env('WINDIR'))
            self.username = os.getenv('USERNAME')
        else:
            self.username = os.getenv('USER')

    def __str__(self):
        str_dict = json.loads(super(HostOS, self).__str__())
        if self.is_win():
            str_dict['username'] = self.username
        return json.dumps(str_dict)

class AndroidOS(OS):
    def __init__(self, device):
        version = device.get_prop('ro.build.version.release')
        super(AndroidOS, self).__init__('android', version)


class Browser(object):
    def __init__(self, name, path, options, os):
        self.name = name
        self.os = os
        self.version = ''
        self._logger = Util.get_logger()

        # path
        if path:
            self.path = Util.use_slash(path)
        elif self.os.is_android():
            if self.name == 'chrome_stable' or self.name == 'chrome':
                self.path = '/data/app/com.android.chrome-1'
        elif self.os.is_cros():
            self.path = '/opt/google/chrome/chrome'
        elif self.os.is_linux():
            if self.name == 'chrome':
                self.path = '/opt/google/chrome/google-chrome'
        elif self.os.is_mac():
            if self.name == 'chrome' or self.name == 'chrome_stable':
                self.path = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
            if self.name == 'chrome_canary':
                self.path = '/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary'
        elif self.os.is_win():
            if self.name == 'chrome' or self.name == 'chrome_stable':
                self.path = '%s/../Local/Google/Chrome/Application/chrome.exe' % self.os.appdata
            if self.name == 'chrome_canary':
                self.path = '%s/../Local/Google/Chrome SxS/Application/chrome.exe' % self.os.appdata
            elif self.name == 'firefox' or self.name == 'firefox_stable':
                self.path = '%s/Mozilla Firefox/firefox.exe' % self.os.programfilesx86
            elif self.name == 'firefox_nightly':
                self.path = '%s/Nightly/firefox.exe' % self.os.programfiles
            elif self.name == 'edge':
                self.path = '%s/systemapps/Microsoft.MicrosoftEdge_8wekyb3d8bbwe/MicrosoftEdge.exe' % self.os.windir
        else:
            Util.not_implemented()

        # option
        self.options = options
        if self.is_chrome():
            if not self.os.is_android() and not self.os.is_cros():
                self.options.append('--disk-cache-size=1')
                if self.os.is_linux():
                    self.options.append('--disk-cache-dir=/dev/null')

            # fullscreen to ensure webdriver can test correctly
            if os.is_linux() or os.is_win():
                self.options.append('--start-maximized')
            # --start-maximized doesn't work on mac
            elif os.is_mac():
                self.options.append('--start-fullscreen')

    def update(self, driver):
        # version
        if not self.os.is_win():
            ua = driver.execute_script('return navigator.userAgent;')
            if self.is_chrome():
                match = re.search('Chrome/(.*) ', ua)
            elif self.is_edge():
                match = re.search('Edge/(.*)$', ua)
            elif self.is_firefox():
                match = re.search('rv:(.*)\)', ua)
            if match:
                self.version = match.group(1)

    def is_chrome(self):
        return self._is_browser('chrome')

    def is_edge(self):
        return self._is_browser('edge')

    def is_firefox(self):
        return self._is_browser('firefox')

    def is_safari(self):
        return self._is_browser('safari')

    def _is_browser(self, name):
        return re.search(name, self.name, re.I)

    def __str__(self):
        return json.dumps({
            'name': self.name,
            'path': self.path,
            'options': ','.join(self.options),
        })


class Webdriver(object):
    CHROME_WEBDRIVER_NAME = 'chromedriver'

    def __init__(self, path, browser, host_os, target_os, android_device=None, debug=False):
        self._logger = Util.get_logger()
        self.path = path
        self.target_os = target_os

        # path
        if target_os.is_cros():
            self.path = '/usr/local/chromedriver/chromedriver'

        executable_suffix = Util.get_executable_suffix(host_os)
        if not self.path and browser.is_chrome() and host_os == target_os:
            if host_os.is_mac():
                browser_dir = browser.path.replace('/Chromium.app/Contents/MacOS/Chromium', '')
            else:
                browser_dir = os.path.dirname(os.path.realpath(browser.path))
            tmp_path = Util.use_slash(browser_dir + '/chromedriver')
            tmp_path += executable_suffix
            if os.path.exists(tmp_path):
                self.path = tmp_path

        if not self.path:
            tmp_path = 'webdriver/%s/' % host_os.name
            if browser.is_chrome():
                tmp_path += self.CHROME_WEBDRIVER_NAME
            elif browser.is_edge():
                tmp_path += self.EDGE_WEBDRIVER_NAME
            elif browser.is_firefox():
                tmp_path += self.FIREFOX_WEBDRIVER_NAME
            tmp_path += executable_suffix
            if os.path.exists(tmp_path):
                self.path = tmp_path

        # webdriver
        if target_os.is_android() or target_os.is_cros():
            # This needs to be done before server process is created
            if target_os.is_cros():
                from telemetry.internal.browser import browser_finder, browser_options
                finder_options = browser_options.BrowserFinderOptions()
                finder_options.browser_type = ('system')
                if browser.options:
                    finder_options.browser_options.AppendExtraBrowserArgs(browser.options)
                finder_options.verbosity = 0
                finder_options.CreateParser().parse_args(args=[])
                b_options = finder_options.browser_options
                b_options.disable_component_extensions_with_background_pages = False
                b_options.create_browser_with_oobe = True
                b_options.clear_enterprise_policy = True
                b_options.dont_override_profile = False
                b_options.disable_gaia_services = True
                b_options.disable_default_apps = True
                b_options.disable_component_extensions_with_background_pages = True
                b_options.auto_login = True
                b_options.gaia_login = False
                b_options.gaia_id = b_options.gaia_id
                open('/mnt/stateful_partition/etc/collect_chrome_crashes', 'w').close()
                browser_to_create = browser_finder.FindBrowser(finder_options)
                self._browser = browser_to_create.Create(finder_options)
                self._browser.tabs[0].Close()

            webdriver_args = [self.path]
            port = self._get_unused_port()
            webdriver_args.append('--port=%d' % port)
            self.server_process = subprocess.Popen(webdriver_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE, env=None)
            capabilities = {}
            capabilities['chromeOptions'] = {}
            self.server_url = 'http://localhost:%d' % port

            if target_os.is_android():
                capabilities['chromeOptions']['androidDeviceSerial'] = android_device.id
                capabilities['chromeOptions']['androidPackage'] = self.ANDROID_CHROME_NAME_PKG[browser.name]
                capabilities['chromeOptions']['args'] = browser.options
            elif target_os.is_cros():
                remote_port = self._get_chrome_remote_debugging_port()
                urllib2.urlopen('http://localhost:%i/json/new' % remote_port)
                capabilities['chromeOptions']['debuggerAddress'] = ('localhost:%d' % remote_port)

            self.driver = webdriver.Remote(command_executor=self.server_url, desired_capabilities=capabilities)
        # other OS
        else:
            chrome_options = selenium.webdriver.ChromeOptions()
            for option in browser.options:
                chrome_options.add_argument(option)
                chrome_options.binary_location = browser.path
            if debug:
                service_args = ['--verbose', '--log-path=log/chromedriver.log']
            else:
                service_args = []
            self.driver = selenium.webdriver.Chrome(executable_path=self.path, chrome_options=chrome_options, service_args=service_args)

        # check
        if not browser.path:
            Util.error('Could not find browser at %s' % browser.path)
        if not self.path:
            Util.error('Could not find webdriver at %s' % self.path)
        if not self.driver:
            Util.error('Could not get webdriver')

        atexit.register(self._quit)

    def _get_chrome_remote_debugging_port(self):
        chrome_pid = int(subprocess.check_output(['pgrep', '-o', '^chrome$']))
        command = subprocess.check_output(['ps', '-p', str(chrome_pid), '-o', 'command='])
        matches = re.search('--remote-debugging-port=([0-9]+)', command)
        if matches:
            return int(matches.group(1))

    def _get_unused_port(self):
        def try_bind(port, socket_type, socket_proto):
            s = socket.socket(socket.AF_INET, socket_type, socket_proto)
            try:
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind(('', port))
                    return s.getsockname()[1]
                except socket.error:
                    return None
            finally:
                s.close()

        while True:
            port = try_bind(0, socket.SOCK_STREAM, socket.IPPROTO_TCP)
            if port and try_bind(port, socket.SOCK_DGRAM, socket.IPPROTO_UDP):
                return port

    def _quit(self):
        self.driver.quit()
        if self.target_os.is_android() or self.target_os.is_cros():
            try:
                urllib2.urlopen(self.server_url + '/shutdown', timeout=10).close()
            except Exception:
                pass
            self.server_process.stdout.close()
            self.server_process.stderr.close()

            if self.target_os.is_cros():
                self._browser.Close()
                del self._browser


if __name__ == '__main__':
    diag = Diag()



#https://bugs.chromium.org/p/chromium/issues/detail?id=751249 D3D9