from basic_ml_pipeline.stream.connection import open_stream
from basic_ml_pipeline.broker import admin_client

def main():
    r = open_stream()
    print(r.status_code)
    
    print(admin_client.list_topics())
              



if __name__ == "__main__":
    main()
