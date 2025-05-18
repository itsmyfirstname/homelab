from os import environ
import requests_oauthlib

class Config:

    @property
    def x_api_url(self) -> str:
        return  "https://api.twitter.com/2"
    
    @property
    def x_api_auth_url(self) -> str:
        return  "https://api.x.com/oauth2/token"
    
    @property
    def x_api_bearer_token(self) -> str:
        return  environ["X_API_BEARER"]
    @property
    def headers(self) -> dict[str, str]:
        return  {"Authorization": f"Bearer {self.x_api_bearer_token}"}
    
    @property
    def kafka_broker(self) -> list[str]:
        return [environ["KAFKA_BROKER"]]
        
        

CONFIG = Config()