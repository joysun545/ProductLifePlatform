from decimal import Decimal
from types import SimpleNamespace

from django.test import TestCase
from django.utils import timezone

from account.models import Organization, OrganizationRole, OrganizationType, User, UserOrganization
from lifecycle.models import (
    AfterSalesApplication,
    AfterSalesServiceStationApplication,
    AfterSalesServiceStationLiaison,
)
from lifecycle.services.aftersales_service import AfterSalesService
from supply_chain.models import (
    FactoryMaterialLot,
    FactoryMaterialStock,
    Material,
    MaterialSpec,
    OrganizationMaterialLot,
    OrganizationMaterialStock,
)


class AfterSalesServiceStationRecommendationTests(TestCase):
    def setUp(self):
        owner_type, _ = OrganizationType.objects.get_or_create(
            code="OWNER", defaults={"name": "厂家"}
        )
        merchant_type, _ = OrganizationType.objects.get_or_create(
            code="MERCHANT", defaults={"name": "商家"}
        )
        supplier_type, _ = OrganizationType.objects.get_or_create(
            code="SUPPLIER", defaults={"name": "供应商"}
        )
        self.factory = Organization.objects.create(
            code="AFTER-SALES-STATION-FACTORY",
            name="服务站筛选厂家",
            organization_type=owner_type,
        )
        self.supplier = Organization.objects.create(
            code="AFTER-SALES-STATION-SUPPLIER",
            name="服务站筛选供应商",
            organization_type=supplier_type,
            factory_root=self.factory,
        )
        self.electrician_role, _ = OrganizationRole.objects.get_or_create(
            organization_type=merchant_type,
            code="merchant_electrician",
            defaults={"name": "商家电工"},
        )
        self.station_contact = User.objects.create_user(
            username="after_sales_station_contact",
            wx_openid="after-sales-station-contact-openid",
            phone="13900000001",
        )
        self.material_a = Material.objects.create(
            code="AFTER-SALES-STATION-MATERIAL-A", name="测试控制器"
        )
        self.spec_a = MaterialSpec.objects.create(
            material=self.material_a,
            code="AFTER-SALES-STATION-SPEC-A",
            name="测试控制器型号A",
        )
        self.material_b = Material.objects.create(
            code="AFTER-SALES-STATION-MATERIAL-B", name="测试化油器"
        )
        self.spec_b = MaterialSpec.objects.create(
            material=self.material_b,
            code="AFTER-SALES-STATION-SPEC-B",
            name="测试化油器型号B",
        )
        self.request_location = SimpleNamespace(
            factory_organization=self.factory,
            location_latitude=Decimal("29.6000000"),
            location_longitude=Decimal("106.5000000"),
        )

    def test_new_triage_route_options_replace_merchant_electrician_with_service_station(self):
        route_values = {
            value for value, _label in AfterSalesService._triage_dispatch_route_choices()
        }
        self.assertIn(AfterSalesApplication.DispatchRoute.SERVICE_STATION, route_values)
        self.assertNotIn(
            AfterSalesApplication.DispatchRoute.MERCHANT_ELECTRICIAN,
            route_values,
        )

    def _create_station(self, *, suffix, latitude, longitude, factory=None):
        organization = Organization.objects.create(
            code=f"AFTER-SALES-STATION-{suffix}",
            name=f"服务站{suffix}",
            organization_type=self.electrician_role.organization_type,
            factory_root=factory or self.factory,
            is_active=True,
        )
        contact = User.objects.create_user(
            username=f"after_sales_station_contact_{suffix}",
            wx_openid=f"after-sales-station-contact-{suffix}-openid",
            phone="13900000002",
        )
        identity = UserOrganization.objects.create(
            user=contact,
            organization=organization,
            role=self.electrician_role,
        )
        station = AfterSalesServiceStationApplication.objects.create(
            organization=organization,
            submitted_by=contact,
            status=AfterSalesServiceStationApplication.Status.APPROVED,
            service_location_name=f"{suffix}维修点",
            service_location_address=f"{suffix}服务地址",
            service_latitude=Decimal(str(latitude)),
            service_longitude=Decimal(str(longitude)),
            location_captured_at=timezone.now(),
            receiving_address=f"{suffix}收件地址",
            receiving_contact_name="服务联系人",
            receiving_contact_phone=contact.phone,
            notice_agreed_at=timezone.now(),
        )
        AfterSalesServiceStationLiaison.objects.create(
            application=station,
            identity=identity,
            member_name_snapshot="服务联系人",
            role_code_snapshot=self.electrician_role.code,
            role_name_snapshot=self.electrician_role.name,
        )
        return station

    def _add_station_stock(self, *, station, spec, suffix, on_hand=1000, reserved=0):
        stock = OrganizationMaterialStock.objects.create(
            organization=station.organization,
            source_factory=self.factory,
            supplier=self.supplier,
            material_spec=spec,
            unit="件",
        )
        source_stock, _ = FactoryMaterialStock.objects.get_or_create(
            factory=self.factory,
            supplier=self.supplier,
            material_spec=spec,
            defaults={"unit": "件"},
        )
        source_lot = FactoryMaterialLot.objects.create(
            stock=source_stock,
            batch_code=f"AS-STATION-SOURCE-{suffix}",
            received_milli=max(1, on_hand),
            on_hand_milli=max(1, on_hand),
            received_by=self.station_contact,
        )
        OrganizationMaterialLot.objects.create(
            stock=stock,
            source_factory_lot=source_lot,
            source_reference=f"AS-STATION-LOT-{suffix}",
            source_order_plan_id=1,
            source_shipment_id=1,
            source_accessory_item_id=1,
            source_batch_code_snapshot=source_lot.batch_code,
            received_milli=max(1, on_hand),
            on_hand_milli=on_hand,
            reserved_milli=reserved,
            received_by=self.station_contact,
            received_at=timezone.now(),
        )

    def test_nearest_three_within_fifty_kilometers_are_recommended(self):
        stations = [
            self._create_station(suffix=str(index), latitude=29.60 + index * 0.04, longitude=106.50)
            for index in range(1, 5)
        ]
        recommendations, message = AfterSalesService._service_station_recommendations(
            self.request_location, []
        )

        self.assertEqual(
            [row["station_application"].pk for row in recommendations],
            [station.pk for station in stations[:3]],
        )
        self.assertIn("最近的 3 家", message)

    def test_stations_from_other_manufacturer_are_excluded_before_distance_ranking(self):
        other_factory = Organization.objects.create(
            code="AFTER-SALES-OTHER-FACTORY",
            name="其他厂家",
            organization_type=self.factory.organization_type,
        )
        self._create_station(
            suffix="FOREIGN-CLOSE",
            latitude=29.6001,
            longitude=106.5001,
            factory=other_factory,
        )
        same_factory = self._create_station(
            suffix="SAME-FACTORY",
            latitude=29.65,
            longitude=106.50,
        )

        recommendations, _message = AfterSalesService._service_station_recommendations(
            self.request_location, []
        )

        self.assertEqual(
            [row["station_application"].pk for row in recommendations],
            [same_factory.pk],
        )

    def test_material_filter_requires_every_selected_model_and_usable_stock(self):
        only_a = self._create_station(suffix="ONLY-A", latitude=29.61, longitude=106.50)
        both = self._create_station(suffix="BOTH", latitude=29.62, longitude=106.50)
        reserved = self._create_station(suffix="RESERVED", latitude=29.63, longitude=106.50)
        self._add_station_stock(station=only_a, spec=self.spec_a, suffix="ONLY-A")
        self._add_station_stock(station=both, spec=self.spec_a, suffix="BOTH-A")
        self._add_station_stock(station=both, spec=self.spec_b, suffix="BOTH-B")
        self._add_station_stock(
            station=reserved,
            spec=self.spec_a,
            suffix="RESERVED-A",
            on_hand=1000,
            reserved=1000,
        )
        self._add_station_stock(
            station=reserved,
            spec=self.spec_b,
            suffix="RESERVED-B",
            on_hand=1000,
            reserved=1000,
        )

        recommendations, _message = AfterSalesService._service_station_recommendations(
            self.request_location,
            [
                {"specification_id": self.spec_a.pk, "material_name": "测试控制器"},
                {"specification_id": self.spec_b.pk, "material_name": "测试化油器"},
            ],
        )

        self.assertEqual(
            [row["station_application"].pk for row in recommendations],
            [both.pk],
        )

    def test_when_all_matching_stations_are_over_fifty_kilometers_return_only_nearest(self):
        nearest = self._create_station(suffix="FAR-NEAREST", latitude=30.30, longitude=106.50)
        self._create_station(suffix="FARTHER", latitude=31.00, longitude=106.50)
        recommendations, message = AfterSalesService._service_station_recommendations(
            self.request_location, []
        )

        self.assertEqual(len(recommendations), 1)
        self.assertEqual(recommendations[0]["station_application"].pk, nearest.pk)
        self.assertIn("均超过 50 公里", message)

    def test_one_or_two_stations_within_fifty_kilometers_are_not_filled_from_farther_stations(self):
        near_one = self._create_station(suffix="NEAR-ONE", latitude=29.64, longitude=106.50)
        near_two = self._create_station(suffix="NEAR-TWO", latitude=29.68, longitude=106.50)
        self._create_station(suffix="OUTSIDE-RANGE", latitude=30.30, longitude=106.50)

        recommendations, message = AfterSalesService._service_station_recommendations(
            self.request_location, []
        )

        self.assertEqual(
            [row["station_application"].pk for row in recommendations],
            [near_one.pk, near_two.pk],
        )
        self.assertIn("最近的 2 家", message)
