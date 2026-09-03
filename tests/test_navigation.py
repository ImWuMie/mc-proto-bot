from __future__ import annotations

import inspect
import unittest

from protobot.client import Bot
from protobot.navigation import FlightPathfinder, _FlightNode
from protobot.physics import StaticCollisionWorld, Vec3


class FlightPathQualityTests(unittest.TestCase):
    def test_open_air_route_is_one_continuous_flight_node(self) -> None:
        planner = FlightPathfinder(StaticCollisionWorld())
        target = Vec3(80.5, 82.0, -30.5)

        path = planner.find_path(Vec3(0.5, 70.0, 0.5), target)

        self.assertEqual(path.operations, ("fly",))
        self.assertEqual(path.waypoints[0].position, target)
        self.assertEqual(path.explored_nodes, 0)

    def test_route_cost_prefers_straight_flight(self) -> None:
        current = _FlightNode(0, 140, 0, move_x=1)

        straight = FlightPathfinder._flight_edge_cost(
            current, 1, 0, 0, vclip_edge=False
        )
        turn = FlightPathfinder._flight_edge_cost(
            current, 0, 0, 1, vclip_edge=False
        )
        reverse = FlightPathfinder._flight_edge_cost(
            current, -1, 0, 0, vclip_edge=False
        )

        self.assertLess(straight, turn)
        self.assertLess(turn, reverse)

    def test_route_cost_penalizes_vclip_entry(self) -> None:
        current = _FlightNode(0, 140, 0)

        normal = FlightPathfinder._flight_edge_cost(
            current, 0, 1, 0, vclip_edge=False
        )
        clipped = FlightPathfinder._flight_edge_cost(
            current, 0, 1, 0, vclip_edge=True
        )

        self.assertGreater(clipped, normal + 2.0)

    def test_continuous_vclip_is_merged_into_one_action(self) -> None:
        world = StaticCollisionWorld()
        for x in range(-2, 3):
            for z in range(-2, 3):
                world.add_block(x, 2, z)
        planner = FlightPathfinder(world, vclip=True, vclip_up_limit=3.0)

        path = planner.find_path(Vec3(0.5, 0.0, 0.5), Vec3(0.5, 3.0, 0.5))

        self.assertEqual(path.operations, ("vclip",))
        self.assertEqual(path.waypoints[0].position, Vec3(0.5, 3.0, 0.5))

    def test_fly_to_defaults_to_complete_quality_plan(self) -> None:
        parameters = inspect.signature(Bot.fly_to).parameters

        self.assertIs(parameters["realtime"].default, False)
        self.assertEqual(parameters["planning_horizon"].default, 32.0)
        self.assertEqual(parameters["max_nodes"].default, 65536)
        self.assertEqual(parameters["timeout"].default, 180.0)


if __name__ == "__main__":
    unittest.main()
