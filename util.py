import inspect
import winreg

class Overlay():
    @classmethod
    def print(cls):
        print('[%s]' % __class__.__name__)
        cls._print(winreg.HKEY_LOCAL_MACHINE, 'SYSTEM\CurrentControlSet\Control\GraphicsDrivers', 'DisableOverlays')
        cls._print(winreg.HKEY_LOCAL_MACHINE, 'SOFTWARE\Microsoft\Windows\Dwm', 'OverlayTestMode')

    @classmethod
    def _print(cls, key, sub_key, value_name):
        print('(%s, %s, %s): %s' % (key, sub_key, value_name, Registry.query(key, sub_key, value_name)))


class Registry():
    @classmethod
    def query(cls, key, sub_key, value_name):
        try:
            reg_key = winreg.ConnectRegistry(None, key)
            reg_sub_key = winreg.OpenKeyEx(reg_key, sub_key)
            return winreg.QueryValueEx(reg_sub_key, value_name)
        except OSError:
            return (None, None)

class Util():
    @classmethod
    def info(cls, msg):
        cls._msg(msg)

    @classmethod
    def warning(cls, msg):
        cls._msg(msg, show_strace=True)

    @classmethod
    def error(cls, msg, abort=True, error_code=1):
        cls._msg(msg, show_strace=True)
        if abort:
            quit(error_code)

    @classmethod
    def cmd(cls, msg):
        cls._msg(msg)

    @classmethod
    def _msg(cls, msg, show_strace=False):
        m = inspect.stack()[1][3].upper()
        if show_strace:
            m += ', File "%s", Line: %s, Function %s' % inspect.stack()[2][1:4]
        m = '[' + m + '] ' + msg
        print(m)