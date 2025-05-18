from kafka.admin import KafkaAdminClient, NewTopic
from basic_ml_pipeline.config import CONFIG

admin_client = KafkaAdminClient(bootstrap_servers=CONFIG.kafka_broker)

topics = [
    NewTopic(name="tweets", num_partitions=1, replication_factor=1),
    NewTopic(name="errors", num_partitions=1, replication_factor=1),
]

for topic in topics:
    print(admin_client.create_topics(topics))