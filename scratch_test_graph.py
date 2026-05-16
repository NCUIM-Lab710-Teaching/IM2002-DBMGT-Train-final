import sys
sys.path.insert(0, ".")
from databases.graph.queries import (
    query_shortest_route,
    query_cheapest_route,
    query_alternative_routes,
    query_interchange_path,
    query_delay_ripple,
    query_station_connections
)

def test():
    print("Shortest:", query_shortest_route("MS01", "MS14", "metro"))
    print("Cheapest:", query_cheapest_route("MS01", "MS14", "metro"))
    print("Alternative:", query_alternative_routes("NR01", "NR05", "NR03", "rail"))
    print("Interchange:", query_interchange_path("MS01", "NR05"))
    print("Delay Ripple:", query_delay_ripple("MS05", 1))
    print("Connections:", query_station_connections("MS01"))

if __name__ == "__main__":
    test()
