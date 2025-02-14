import sys
import threading

from discovery.service.service import AbstractPropertyBuilder
from discovery.utils.constants import DEFAULT_KEY
from discovery.utils.inventory import CPInventoryManager
from discovery.utils.services import ConfluentServices, ServiceData
from discovery.utils.utils import InputContext, Logger, FileUtils

logger = Logger.get_logger()

class_name = ""


class KafkaConnectServicePropertyBuilder:

    @staticmethod
    def build_properties(input_context: InputContext, inventory: CPInventoryManager):
        from discovery.service import get_service_builder_class
        builder_class = get_service_builder_class(modules=sys.modules[__name__],
                                                  default_class_name="KafkaConnectServicePropertyBaseBuilder",
                                                  version=input_context.from_version)
        global class_name
        class_name = builder_class
        builder_class(input_context, inventory).build_properties()


class KafkaConnectServicePropertyBaseBuilder(AbstractPropertyBuilder):
    inventory = None
    input_context = None
    hosts = []

    def __init__(self, input_context: InputContext, inventory: CPInventoryManager):
        self.inventory = inventory
        self.input_context = input_context
        self.mapped_service_properties = set()
        self.service = ConfluentServices(input_context).KAFKA_CONNECT()
        self.group = self.service.group

    def build_properties(self):
        # Get the hosts for given service
        hosts = self.get_service_host(self.service, self.inventory)
        self.hosts = hosts
        if not hosts:
            logger.error(f"Could not find any host with service {self.service.name} ")
            return

        host_service_properties = self.get_property_mappings(self.input_context, self.service, hosts)

        # Workers running on different hosts
        if self.input_context.multi_threaded:
            logger.debug(f'Running in multithreaded environment...')
            threads = list()
            for host in hosts:
                t = threading.Thread(target=self.build_properties_threaded, args=(host, host_service_properties,))
                threads.append(t)

            for thread in threads:
                thread.start()
                thread.join()
        else:
            for host in hosts:
                self.build_properties_threaded(host, host_service_properties)

    def build_properties_threaded(self, host, host_service_properties):

        # reset the mapped service properties
        self.mapped_service_properties = set()

        service_properties = host_service_properties.get(host).get(DEFAULT_KEY)

        # check if group id exists
        key = "group.id"
        if key in service_properties:
            self.group = service_properties.get(key)
            self.inventory.add_group(self.group)
            self.inventory.add_child(self.service.group, self.group)

            # Remove host from main group and add in child group
            self.inventory.add_host(host, self.group)
            self.update_inventory(self.inventory, (self.group, {'kafka_connect_group_id': self.group}))

        # Build service user group properties
        self.__build_daemon_properties(self.input_context, self.service, [host])

        # Build service properties
        self.__build_service_properties(service_properties)

        # Add custom properties of Kafka broker
        self.__build_custom_properties(host_service_properties, self.mapped_service_properties)

        # Build Command line properties
        self.__build_runtime_properties([host])

    def __build_daemon_properties(self, input_context: InputContext, service: ServiceData, hosts: list):

        # User group information
        response = self.get_service_user_group(input_context, service, hosts)
        self.update_inventory(self.inventory, (self.group, response[1]))

    def __build_service_properties(self, service_properties):
        for key, value in vars(class_name).items():
            if callable(getattr(class_name, key)) and key.startswith("_build"):
                func = getattr(class_name, key)
                logger.info(f"Calling KafkaConnect property builder.. {func.__name__}")
                result = func(self, service_properties)
                self.update_inventory(self.inventory, result)

    def __build_custom_properties(self, host_service_properties: dict, mapped_properties: set):
        custom_group = "kafka_connect_custom_properties"
        skip_properties = set(FileUtils.get_kafka_connect_configs("skip_properties"))

        _host_service_properties = dict()
        for host in host_service_properties.keys():
            _host_service_properties[host] = host_service_properties.get(host).get(DEFAULT_KEY)
        self.build_custom_properties(inventory=self.inventory, group=self.group,
                                     custom_properties_group_name=custom_group,
                                     host_service_properties=_host_service_properties, skip_properties=skip_properties,
                                     mapped_properties=mapped_properties)

    def __build_runtime_properties(self, hosts: list):
        # Build Java runtime overrides
        data = (self.group,
                {'kafka_connect_custom_java_args': self.get_jvm_arguments(self.input_context, self.service, hosts)})
        self.update_inventory(self.inventory, data)

    def _build_service_replication_factor(self, service_prop: dict) -> tuple:
        key = "config.storage.replication.factor"
        self.mapped_service_properties.add(key)
        value = service_prop.get(key)
        if value is not None:
            return self.group, {"kafka_connect_default_internal_replication_factor": int(value)}
        return self.group, {}

    def _build_config_storage_topic(self, service_prop: dict) -> tuple:
        key = "config.storage.topic"
        self.mapped_service_properties.add(key)
        value = service_prop.get(key)
        if value is not None:
            return self.group, {"kafka_connect_group_id": value.rstrip("-configs")}
        return self.group, {}

    def _build_monitoring_interceptor_propperty(self, service_prop: dict) -> tuple:
        key = "confluent.monitoring.interceptor.topic"
        self.mapped_service_properties.add(key)
        return self.group, {"kafka_connect_monitoring_interceptors_enabled": key in service_prop}

    def _build_service_protocol_port(self, service_prop: dict) -> tuple:
        key = "listeners"
        self.mapped_service_properties.add(key)
        from urllib.parse import urlparse
        listener = service_prop.get(key).split(',')[0]
        parsed_uri = urlparse(listener)
        return self.group, {
            "kafka_connect_http_protocol": parsed_uri.scheme,
            "kafka_connect_rest_port": parsed_uri.port
        }

    def _build_advertised_protocol_port(self, service_prop: dict) -> tuple:

        properties_dict = dict()

        key = "rest.advertised.listener"
        self.mapped_service_properties.add(key)
        if key in service_prop:
            properties_dict['kafka_connect_http_protocol'] = service_prop.get(key)

        key = "rest.port"
        self.mapped_service_properties.add(key)
        if key in service_prop:
            properties_dict['kafka_connect_rest_port'] = int(service_prop.get(key))

        key = 'rest.advertised.port'
        self.mapped_service_properties.add(key)
        if key in service_prop:
            properties_dict['kafka_connect_replicator_port'] = int(service_prop.get(key))

        return self.group, properties_dict

    def _build_ssl_client_properties(self, service_properties: dict) -> tuple:
        property_dict = dict()
        key = 'ssl.truststore.location'
        self.mapped_service_properties.add(key)
        if key in service_properties:
            property_dict['kafka_connect_truststore_path'] = service_properties.get(key)

        key = 'ssl.truststore.password'
        self.mapped_service_properties.add(key)
        if key in service_properties:
            property_dict['kafka_connect_truststore_storepass'] = service_properties.get(key)

        key = 'ssl.keystore.location'
        self.mapped_service_properties.add(key)
        if key in service_properties:
            property_dict['kafka_connect_keystore_path'] = service_properties.get(key)

        key = 'ssl.keystore.password'
        self.mapped_service_properties.add(key)
        if key in service_properties:
            property_dict['kafka_connect_keystore_storepass'] = service_properties.get(key)

        key = 'ssl.key.password'
        self.mapped_service_properties.add(key)
        if key in service_properties:
            property_dict['kafka_connect_keystore_keypass'] = service_properties.get(key)

        return self.group, property_dict

    def _build_ssl_properties(self, service_properties: dict) -> tuple:
        key = 'rest.advertised.listener'
        kafka_connect_http_protocol = service_properties.get(key)
        if kafka_connect_http_protocol != 'https':
            return self.group, {}

        property_list = ["listeners.https.ssl.keystore.location", "listeners.https.ssl.keystore.password",
                         "listeners.https.ssl.key.password",
                         "listeners.https.ssl.truststore.location", "listeners.https.ssl.truststore.password"]

        for property_key in property_list:
            self.mapped_service_properties.add(property_key)

        property_dict = dict()

        property_dict['ssl_enabled'] = True
        property_dict['ssl_provided_keystore_and_truststore'] = True
        property_dict['ssl_provided_keystore_and_truststore_remote_src'] = True
        property_dict['kafka_connect_keystore_path'] = service_properties.get('listeners.https.ssl.keystore.location')
        property_dict['ssl_keystore_store_password'] = service_properties.get('listeners.https.ssl.keystore.password')
        property_dict['ssl_keystore_key_password'] = service_properties.get('listeners.https.ssl.key.password')
        property_dict['kafka_connect_truststore_path'] = service_properties.get(
            'listeners.https.ssl.truststore.location')
        property_dict['ssl_truststore_password'] = service_properties.get('listeners.https.ssl.truststore.password')
        property_dict['ssl_truststore_ca_cert_alias'] = ''

        keystore_aliases = self.get_keystore_alias_names(input_context=self.input_context,
                                                         keystorepass=property_dict['ssl_keystore_store_password'],
                                                         keystorepath=property_dict['kafka_connect_keystore_path'],
                                                         hosts=self.hosts)
        truststore_aliases = self.get_keystore_alias_names(input_context=self.input_context,
                                                           keystorepass=property_dict['ssl_truststore_password'],
                                                           keystorepath=property_dict['kafka_connect_truststore_path'],
                                                           hosts=self.hosts)
        if keystore_aliases:
            # Set the first alias name
            property_dict["ssl_keystore_alias"] = keystore_aliases[0]
        if truststore_aliases:
            property_dict["ssl_truststore_ca_cert_alias"] = truststore_aliases[0]

        return self.group, property_dict

    def _build_mtls_property(self, service_properties: dict) -> tuple:
        key = 'listeners.https.ssl.client.auth'
        self.mapped_service_properties.add(key)
        value = service_properties.get(key)
        if value is not None and value == 'required':
            return self.group, {'kafka_connect_authentication_type': 'mtls'}
        return self.group, {}

    def _build_rbac_properties(self, service_prop: dict) -> tuple:
        key1 = 'rest.servlet.initializor.classes'
        if service_prop.get(key1) is None:
            return self.group, {'rbac_enabled': False}
        property_dict = dict()
        key2 = 'public.key.path'
        key3 = 'confluent.metadata.bootstrap.server.urls'
        property_dict['rbac_enabled'] = True
        property_dict['rbac_enabled_public_pem_path'] = service_prop.get(key2)
        self.mapped_service_properties.add(key1)
        self.mapped_service_properties.add(key2)
        self.mapped_service_properties.add(key3)
        return self.group, property_dict

    def _build_ldap_properties(self, service_prop: dict) -> tuple:
        property_dict = dict()
        key = 'confluent.metadata.basic.auth.user.info'
        self.mapped_service_properties.add(key)
        if service_prop.get(key) is not None:
            metadata_user_info = service_prop.get(key)
            property_dict['kafka_connect_ldap_user'] = metadata_user_info.split(':')[0]
            property_dict['kafka_connect_ldap_password'] = metadata_user_info.split(':')[1]
        return self.group, property_dict

    def _build_secret_registry_properties(self, service_prop: dict) -> tuple:
        key1 = 'config.providers'
        if service_prop.get(key1) is None:
            return self.group, {'kafka_connect_secret_registry_enabled': False}
        key2 = 'config.providers.secret.param.master.encryption.key'
        key3 = 'config.providers.secret.param.kafkastore.topic.replication.factor'
        property_dict = dict()
        property_dict['kafka_connect_secret_registry_enabled'] = True
        property_dict['kafka_connect_secret_registry_key'] = service_prop.get(key2)
        if service_prop.get(key3) is not None:
            property_dict['kafka_connect_secret_registry_default_replication_factor'] = int(service_prop.get(key3))
        self.mapped_service_properties.add(key1)
        self.mapped_service_properties.add(key2)
        self.mapped_service_properties.add(key3)
        return self.group, property_dict

    def _build_telemetry_properties(self, service_prop: dict) -> tuple:
        property_dict = self.build_telemetry_properties(service_prop)
        return self.group, property_dict

    def _build_log4j_properties(self, service_properties: dict) -> tuple:
        log4j_file = self.get_log_file_path(self.input_context, self.service, self.hosts, "KAFKA_LOG4J_OPTS")
        default_log4j_file = "/etc/kafka/connect-log4j.properties"
        root_logger, file = self.get_root_logger(self.input_context, self.hosts, log4j_file, default_log4j_file)

        if root_logger is None or file is None:
            return self.group, {'kafka_connect_custom_log4j': False}

        return self.group, {
            'log4j_file': file,
            'kafka_connect_log4j_root_logger': root_logger
        }

    def _build_jmx_properties(self, service_properties: dict) -> tuple:
        monitoring_details = self.get_monitoring_details(self.input_context, self.service, self.hosts, 'KAFKA_OPTS')
        service_monitoring_details = dict()
        group_name = self.group

        for key, value in monitoring_details.items():
            service_monitoring_details[f"{group_name}_{key}"] = value

        return group_name, service_monitoring_details

    def _build_kerberos_properties(self, service_prop: dict) -> tuple:
        key1 = 'sasl.jaas.config'
        key2 = 'producer.sasl.jaas.config'
        key3 = 'consumer.sasl.jaas.config'
        self.mapped_service_properties.add(key1)
        self.mapped_service_properties.add(key2)
        self.mapped_service_properties.add(key3)

        sasl_config = ""
        if service_prop.get(key1) is not None:
            sasl_config = service_prop.get(key1)
        elif service_prop.get(key2) is not None:
            sasl_config = service_prop.get(key2)
        elif service_prop.get(key3) is not None:
            sasl_config = service_prop.get(key3)
        else:
            return "all", {}

        try:
            keytab = sasl_config.split('keyTab="')[1].split('"')[0]
            principal = sasl_config.split('principal="')[1].split('"')[0]
        except IndexError as e:
            keytab = ""
            principal = ""
        if keytab != "" or principal != "":
            return self.group, {
                'sasl_protocol': 'kerberos',
                'kafka_connect_kerberos_principal': principal,
                'kafka_connect_kerberos_keytab_path': keytab
            }
        return 'all', {}


class KafkaConnectServicePropertyBaseBuilder60(KafkaConnectServicePropertyBaseBuilder):
    pass


class KafkaConnectServicePropertyBaseBuilder61(KafkaConnectServicePropertyBaseBuilder):
    pass


class KafkaConnectServicePropertyBaseBuilder62(KafkaConnectServicePropertyBaseBuilder):
    pass


class KafkaConnectServicePropertyBaseBuilder70(KafkaConnectServicePropertyBaseBuilder):
    pass


class KafkaConnectServicePropertyBaseBuilder71(KafkaConnectServicePropertyBaseBuilder):
    pass


class KafkaConnectServicePropertyBaseBuilder72(KafkaConnectServicePropertyBaseBuilder):
    pass
