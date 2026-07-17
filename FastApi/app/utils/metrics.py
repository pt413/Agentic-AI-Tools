from prometheus_client import Counter, Histogram

conversation_requests_total = Counter(
    "conversation_requests_total",
    "Total conversation service calls",
)

conversation_failures_total = Counter(
    "conversation_failures_total",
    "Failed conversation service calls",
)

conversation_duration_ms = Histogram(
    "conversation_duration_ms",
    "Conversation query duration in ms",
    buckets=(50, 100, 200, 300, 500, 1000, 2000),
)

conversation_rows_returned = Histogram(
    "conversation_rows_returned",
    "Rows returned per conversation query",
    buckets=(0, 1, 5, 10, 25, 50, 100),
)

conversation_db_query_duration_ms = Histogram(
    "conversation_db_query_duration_ms",
    "DB query duration for conversation (query.all) in ms",
    buckets=(10, 25, 50, 100, 200, 300, 500, 1000, 2000),
)

conversation_resolver_duration_ms = Histogram(
    "conversation_resolver_duration_ms",
    "DB lookup duration for conversation resolvers (UserData.*) in ms",
    buckets=(5, 10, 25, 50, 100, 200, 300, 500, 1000, 2000),
)

grpc_get_conversation_requests_total = Counter(
    "grpc_get_conversation_requests_total",
    "Total gRPC GetConversation client calls",
)

grpc_get_conversation_failures_total = Counter(
    "grpc_get_conversation_failures_total",
    "Failed gRPC GetConversation client calls",
)

grpc_get_conversation_duration_ms = Histogram(
    "grpc_get_conversation_duration_ms",
    "gRPC GetConversation client call duration in ms",
    buckets=(5, 10, 25, 50, 100, 200, 300, 500, 1000, 2000),
)

grpc_server_get_conversation_requests_total = Counter(
    "grpc_server_get_conversation_requests_total",
    "Total gRPC server GetConversation calls",
)

grpc_server_get_conversation_failures_total = Counter(
    "grpc_server_get_conversation_failures_total",
    "Failed gRPC server GetConversation calls",
)

grpc_server_get_conversation_duration_ms = Histogram(
    "grpc_server_get_conversation_duration_ms",
    "gRPC server GetConversation handler duration in ms",
    buckets=(5, 10, 25, 50, 100, 200, 300, 500, 1000, 2000, 5000),
)

grpc_server_get_conversation_mapping_duration_ms = Histogram(
    "grpc_server_get_conversation_mapping_duration_ms",
    "gRPC server GetConversation duration to map DB rows to protobuf response in ms",
    buckets=(1, 2, 5, 10, 25, 50, 100, 200, 300, 500),
)
