import yaml

from BusinessLogic.interfaces.Iuser_Interface import IUserInterface
from DataTypes.login_types import LogInData


class UserInterfaceLogin(IUserInterface):
    def __init__(self, key):
        self.data = None
        self.__set_data(key)

    def get_data(self):
        return self.data

    def __set_data(self, key):
        with open(key, encoding="utf-8") as login_file:
            config = yaml.safe_load(login_file)

        if not isinstance(config, dict):
            raise ValueError("login YAML must contain a mapping of login fields")

        values = {
            "broker": config.get("broker", ""),
            "user_id": config.get("user_id", config.get("user_name", "")),
            "password": config.get("password", config.get("pin", "")),
            "api_key": config.get("api_key", config.get("client_id", "")),
            "api_secret_key": config.get("api_secret_key", config.get("secret_id", "")),
            "phone_no": config.get("phone_no", ""),
            "totp_key": config.get("totp_key", config.get("totp", "")),
        }
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise ValueError(f"login YAML field '{name}' must be a string or integer")
            values[name] = str(value)

        if not values["broker"].strip():
            raise ValueError("login YAML is missing required field 'broker'")
        if not values["user_id"].strip():
            raise ValueError("login YAML is missing required field 'user_id'")

        self.data = LogInData(**values)
