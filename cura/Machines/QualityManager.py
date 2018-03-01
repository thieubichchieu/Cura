# Copyright (c) 2018 Ultimaker B.V.
# Cura is released under the terms of the LGPLv3 or higher.

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from UM.Application import Application
from UM.Logger import Logger
from UM.Util import parseBool

from .QualityGroup import QualityGroup
from .QualityNode import QualityNode

#
# Quality lookup tree structure:
#
#      <machine_definition_id>------|
#                |                  |
#          <variant_name>        <root_material_id>
#                |
#        <root_material_id>
#                |
#          <quality_type>
#                |
#            <quality_name>
#       + <quality_changes_name>
#


#
# Similar to MaterialManager, QualityManager maintains a number of maps and trees for material lookup.
# The models GUI and QML use are now only dependent on the QualityManager. That means as long as the data in
# QualityManager gets updated correctly, the GUI models should be updated correctly too, and the same goes for GUI.
#
# For now, updating the lookup maps and trees here is very simple: we discard the old data completely and recreate them
# again. This means the update is exactly the same as initialization. There are performance concerns about this approach
# but so far the creation of the tables and maps is very fast and there is no noticeable slowness, we keep it like this
# because it's simple.
#
class QualityManager(QObject):

    qualitiesUpdated = pyqtSignal()

    def __init__(self, container_registry, parent = None):
        super().__init__(parent)
        self._application = Application.getInstance()
        self._material_manager = self._application.getMaterialManager()
        self._container_registry = container_registry

        self._empty_quality_container = self._application.empty_quality_container
        self._empty_quality_changes_container = self._application.empty_quality_changes_container

        self._machine_variant_material_quality_type_to_quality_dict = {}  # for quality lookup
        self._machine_quality_type_to_quality_changes_dict = {}  # for quality_changes lookup

        self._default_machine_definition_id = "fdmprinter"

        self._container_registry.containerMetaDataChanged.connect(self._onContainerMetadataChanged)
        self._container_registry.containerAdded.connect(self._onContainerMetadataChanged)
        self._container_registry.containerRemoved.connect(self._onContainerMetadataChanged)

        # When a custom quality gets added/imported, there can be more than one InstanceContainers. In those cases,
        # we don't want to react on every container/metadata changed signal. The timer here is to buffer it a bit so
        # we don't react too many time.
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(300)
        self._update_timer.setSingleShot(True)
        self._update_timer.timeout.connect(self._updateMaps)

    def initialize(self):
        # Initialize the lookup tree for quality profiles with following structure:
        # <machine> -> <variant> -> <material>
        #           -> <material>

        self._machine_variant_material_quality_type_to_quality_dict = {}  # for quality lookup
        self._machine_quality_type_to_quality_changes_dict = {}  # for quality_changes lookup

        quality_metadata_list = self._container_registry.findContainersMetadata(type = "quality")
        for metadata in quality_metadata_list:
            if metadata["id"] == "empty_quality":
                continue

            definition_id = metadata["definition"]
            quality_type = metadata["quality_type"]

            root_material_id = metadata.get("material")
            variant_name = metadata.get("variant")
            is_global_quality = metadata.get("global_quality", False)
            is_global_quality = is_global_quality or (root_material_id is None and variant_name is None)

            # Sanity check: material+variant and is_global_quality cannot be present at the same time
            if is_global_quality and (root_material_id or variant_name):
                raise RuntimeError("Quality profile [%s] contains invalid data: it is a global quality but contains 'material' and 'nozzle' info." % metadata["id"])

            if definition_id not in self._machine_variant_material_quality_type_to_quality_dict:
                self._machine_variant_material_quality_type_to_quality_dict[definition_id] = QualityNode()
            machine_node = self._machine_variant_material_quality_type_to_quality_dict[definition_id]

            if is_global_quality:
                # For global qualities, save data in the machine node
                machine_node.addQualityMetadata(quality_type, metadata)
                continue

            if variant_name is not None:
                # If variant_name is specified in the quality/quality_changes profile, check if material is specified,
                # too.
                if variant_name not in machine_node.children_map:
                    machine_node.children_map[variant_name] = QualityNode()
                variant_node = machine_node.children_map[variant_name]

                if root_material_id is None:
                    # If only variant_name is specified but material is not, add the quality/quality_changes metadata
                    # into the current variant node.
                    variant_node.addQualityMetadata(quality_type, metadata)
                else:
                    # If only variant_name and material are both specified, go one level deeper: create a material node
                    # under the current variant node, and then add the quality/quality_changes metadata into the
                    # material node.
                    if root_material_id not in variant_node.children_map:
                        variant_node.children_map[root_material_id] = QualityNode()
                    material_node = variant_node.children_map[root_material_id]

                    material_node.addQualityMetadata(quality_type, metadata)

            else:
                # If variant_name is not specified, check if material is specified.
                if root_material_id is not None:
                    if root_material_id not in machine_node.children_map:
                        machine_node.children_map[root_material_id] = QualityNode()
                    material_node = machine_node.children_map[root_material_id]

                    material_node.addQualityMetadata(quality_type, metadata)

        # Initialize the lookup tree for quality_changes profiles with following structure:
        # <machine> -> <quality_type> -> <name>
        quality_changes_metadata_list = self._container_registry.findContainersMetadata(type = "quality_changes")
        for metadata in quality_changes_metadata_list:
            if metadata["id"] == "empty_quality_changes":
                continue

            machine_definition_id = metadata["definition"]
            quality_type = metadata["quality_type"]

            if machine_definition_id not in self._machine_quality_type_to_quality_changes_dict:
                self._machine_quality_type_to_quality_changes_dict[machine_definition_id] = QualityNode()
            machine_node = self._machine_quality_type_to_quality_changes_dict[machine_definition_id]
            machine_node.addQualityChangesMetadata(quality_type, metadata)

        Logger.log("d", "Lookup tables updated.")
        self.qualitiesUpdated.emit()

    def _updateMaps(self):
        self.initialize()

    def _onContainerMetadataChanged(self, container):
        self._onContainerChanged(container)

    def _onContainerChanged(self, container):
        container_type = container.getMetaDataEntry("type")
        if container_type not in ("quality", "quality_changes"):
            return

        # update the cache table
        self._update_timer.start()

    # Updates the given quality groups' availabilities according to which extruders are being used/ enabled.
    def _updateQualityGroupsAvailability(self, machine: "GlobalStack", quality_group_list):
        used_extruders = set()
        # TODO: This will change after the Machine refactoring
        for i in range(machine.getProperty("machine_extruder_count", "value")):
            used_extruders.add(str(i))

        # Update the "is_available" flag for each quality group.
        for quality_group in quality_group_list:
            is_available = True
            if quality_group.node_for_global is None:
                is_available = False
            if is_available:
                for position in used_extruders:
                    if position not in quality_group.nodes_for_extruders:
                        is_available = False
                        break

            quality_group.is_available = is_available

    # Returns a dict of "custom profile name" -> QualityChangesGroup
    def getQualityChangesGroups(self, machine: "GlobalStack") -> dict:
        # Get machine definition ID for quality search
        machine_definition_id = getMachineDefinitionIDForQualitySearch(machine)

        machine_node = self._machine_quality_type_to_quality_changes_dict.get(machine_definition_id)
        if not machine_node:
            Logger.log("i", "Cannot find node for machine def [%s] in QualityChanges lookup table", machine_definition_id)
            return dict()

        # Update availability for each QualityChangesGroup:
        # A custom profile is always available as long as the quality_type it's based on is available
        quality_group_dict = self.getQualityGroups(machine)
        available_quality_type_list = [qt for qt, qg in quality_group_dict.items() if qg.is_available]

        # Iterate over all quality_types in the machine node
        quality_changes_group_dict = dict()
        for quality_type, quality_changes_node in machine_node.quality_type_map.items():
            for quality_changes_name, quality_changes_group in quality_changes_node.children_map.items():
                quality_changes_group_dict[quality_changes_name] = quality_changes_group
                quality_changes_group.is_available = quality_type in available_quality_type_list

        return quality_changes_group_dict

    def getQualityGroups(self, machine: "GlobalStack") -> dict:
        # Get machine definition ID for quality search
        machine_definition_id = getMachineDefinitionIDForQualitySearch(machine)
        # This determines if we should only get the global qualities for the global stack and skip the global qualities for the extruder stacks
        has_variant_materials = parseBool(machine.getMetaDataEntry("has_variant_materials", False))

        # To find the quality container for the GlobalStack, check in the following fall-back manner:
        #   (1) the machine-specific node
        #   (2) the generic node
        machine_node = self._machine_variant_material_quality_type_to_quality_dict.get(machine_definition_id)
        default_machine_node = self._machine_variant_material_quality_type_to_quality_dict.get(self._default_machine_definition_id)
        nodes_to_check = [machine_node, default_machine_node]

        # Iterate over all quality_types in the machine node
        quality_group_dict = {}
        for node in nodes_to_check:
            if node and node.quality_type_map:
                # Only include global qualities
                if has_variant_materials:
                    quality_node = list(node.quality_type_map.values())[0]
                    is_global_quality = parseBool(quality_node.metadata.get("global_quality", False))
                    if not is_global_quality:
                        continue

                for quality_type, quality_node in node.quality_type_map.items():
                    quality_group = QualityGroup(quality_node.metadata["name"], quality_type)
                    quality_group.node_for_global = quality_node
                    quality_group_dict[quality_type] = quality_group
                break

        # Iterate over all extruders to find quality containers for each extruder
        for position, extruder in machine.extruders.items():
            variant_name = None
            if extruder.variant.getId() != "empty_variant":
                variant_name = extruder.variant.getName()

            # This is a list of root material IDs to use for searching for suitable quality profiles.
            # The root material IDs in this list are in prioritized order.
            root_material_id_list = []
            has_material = False  # flag indicating whether this extruder has a material assigned
            if extruder.material.getId() != "empty_material":
                has_material = True
                root_material_id = extruder.material.getMetaDataEntry("base_file")
                # Convert possible generic_pla_175 -> generic_pla
                root_material_id = self._material_manager.getRootMaterialIDWithoutDiameter(root_material_id)
                root_material_id_list.append(root_material_id)

                # Also try to get the fallback material
                material_type = extruder.material.getMetaDataEntry("material")
                fallback_root_material_id = self._material_manager.getFallbackMaterialIdByMaterialType(material_type)
                if fallback_root_material_id:
                    root_material_id_list.append(fallback_root_material_id)

            nodes_to_check = []

            if variant_name:
                # In this case, we have both a specific variant and a specific material
                variant_node = machine_node.getChildNode(variant_name)
                if variant_node and has_material:
                    for root_material_id in root_material_id_list:
                        material_node = variant_node.getChildNode(root_material_id)
                        if material_node:
                            nodes_to_check.append(material_node)
                            break
                nodes_to_check.append(variant_node)

            # In this case, we only have a specific material but NOT a variant
            if has_material:
                for root_material_id in root_material_id_list:
                    material_node = machine_node.getChildNode(root_material_id)
                    if material_node:
                        nodes_to_check.append(material_node)
                        break

            nodes_to_check += [machine_node, default_machine_node]
            for node in nodes_to_check:
                if node and node.quality_type_map:
                    if has_variant_materials:
                        # Only include variant qualities; skip non global qualities
                        quality_node = list(node.quality_type_map.values())[0]
                        is_global_quality = parseBool(quality_node.metadata.get("global_quality", False))
                        if is_global_quality:
                            continue

                    for quality_type, quality_node in node.quality_type_map.items():
                        if quality_type not in quality_group_dict:
                            quality_group = QualityGroup(quality_node.metadata["name"], quality_type)
                            quality_group_dict[quality_type] = quality_group

                        quality_group = quality_group_dict[quality_type]
                        quality_group.nodes_for_extruders[position] = quality_node
                    break

        # Update availabilities for each quality group
        self._updateQualityGroupsAvailability(machine, quality_group_dict.values())

        return quality_group_dict

    def getQualityGroupsForMachineDefinition(self, machine: "GlobalStack") -> dict:
        # Get machine definition ID for quality search
        machine_definition_id = getMachineDefinitionIDForQualitySearch(machine)

        # To find the quality container for the GlobalStack, check in the following fall-back manner:
        #   (1) the machine-specific node
        #   (2) the generic node
        machine_node = self._machine_variant_material_quality_type_to_quality_dict.get(machine_definition_id)
        default_machine_node = self._machine_variant_material_quality_type_to_quality_dict.get(
            self._default_machine_definition_id)
        nodes_to_check = [machine_node, default_machine_node]

        # Iterate over all quality_types in the machine node
        quality_group_dict = dict()
        for node in nodes_to_check:
            if node and node.quality_type_map:
                for quality_type, quality_node in node.quality_type_map.items():
                    quality_group = QualityGroup(quality_node.metadata["name"], quality_type)
                    quality_group.node_for_global = quality_node
                    quality_group_dict[quality_type] = quality_group
                break

        return quality_group_dict


#
# Gets the machine definition ID that can be used to search for Quality containers that are suitable for the given
# machine. The rule is as follows:
#   1. By default, the machine definition ID for quality container search will be "fdmprinter", which is the generic
#      machine.
#   2. If a machine has its own machine quality (with "has_machine_quality = True"), we should use the given machine's
#      own machine definition ID for quality search.
#      Example: for an Ultimaker 3, the definition ID should be "ultimaker3".
#   3. When condition (2) is met, AND the machine has "quality_definition" defined in its definition file, then the
#      definition ID specified in "quality_definition" should be used.
#      Example: for an Ultimaker 3 Extended, it has "quality_definition = ultimaker3". This means Ultimaker 3 Extended
#               shares the same set of qualities profiles as Ultimaker 3.
#
def getMachineDefinitionIDForQualitySearch(machine: "GlobalStack", default_definition_id: str = "fdmprinter") -> str:
    machine_definition_id = default_definition_id
    if parseBool(machine.getMetaDataEntry("has_machine_quality", False)):
        # Only use the machine's own quality definition ID if this machine has machine quality.
        machine_definition_id = machine.getMetaDataEntry("quality_definition")
        if machine_definition_id is None:
            machine_definition_id = machine.definition.getId()

    return machine_definition_id
