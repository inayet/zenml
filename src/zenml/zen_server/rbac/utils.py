#  Copyright (c) ZenML GmbH 2023. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at:
#
#       https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
#  or implied. See the License for the specific language governing
#  permissions and limitations under the License.
"""RBAC utility functions."""

from datetime import datetime
from enum import Enum
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    TypeVar,
    Union,
    cast,
)
from uuid import UUID

from pydantic import BaseModel

from zenml.exceptions import IllegalOperationError
from zenml.models import (
    BaseResponse,
    Page,
    UserScopedResponse,
)
from zenml.models.base_models import BaseResponseModel, UserScopedResponseModel
from zenml.zen_server.auth import get_auth_context
from zenml.zen_server.rbac.models import Action, Resource, ResourceType
from zenml.zen_server.utils import rbac, server_config

AnyOldResponseModel = TypeVar("AnyOldResponseModel", bound=BaseResponseModel)
AnyNewResponseModel = TypeVar(
    "AnyNewResponseModel",
    bound=BaseResponse,  # type: ignore[type-arg]
)
AnyResponseModel = TypeVar(
    "AnyResponseModel",
    bound=Union[BaseResponse, BaseResponseModel],  # type: ignore[type-arg]
)
AnyModel = TypeVar("AnyModel", bound=BaseModel)


def dehydrate_page(
    page: Page[AnyResponseModel],
) -> Page[AnyResponseModel]:
    """Dehydrate all items of a page.

    Args:
        page: The page to dehydrate.

    Returns:
        The page with (potentially) dehydrated items.
    """
    if not server_config().rbac_enabled:
        return page

    auth_context = get_auth_context()
    assert auth_context

    resource_list = [get_subresources_for_model(item) for item in page.items]
    resources = set.union(*resource_list) if resource_list else set()
    permissions = rbac().check_permissions(
        user=auth_context.user, resources=resources, action=Action.READ
    )

    new_items = [
        dehydrate_response_model(item, permissions=permissions)
        for item in page.items
    ]

    return page.copy(update={"items": new_items})


def dehydrate_response_model(
    model: AnyModel, permissions: Optional[Dict[Resource, bool]] = None
) -> AnyModel:
    """Dehydrate a model if necessary.

    Args:
        model: The model to dehydrate.
        permissions: Prefetched permissions that will be used to check whether
            sub-models will be included in the model or not. If a sub-model
            refers to a resource which is not included in this dictionary, the
            permissions will be checked with the RBAC component.

    Returns:
        The (potentially) dehydrated model.
    """
    if not server_config().rbac_enabled:
        return model

    if not permissions:
        auth_context = get_auth_context()
        assert auth_context

        resources = get_subresources_for_model(model)
        permissions = rbac().check_permissions(
            user=auth_context.user, resources=resources, action=Action.READ
        )

    dehydrated_fields = {}

    for field_name in model.__fields__.keys():
        value = getattr(model, field_name)
        dehydrated_fields[field_name] = _dehydrate_value(
            value, permissions=permissions
        )

    return type(model).parse_obj(dehydrated_fields)


def _dehydrate_value(
    value: Any, permissions: Optional[Dict[Resource, bool]] = None
) -> Any:
    """Helper function to recursive dehydrate any object.

    Args:
        value: The value to dehydrate.
        permissions: Prefetched permissions that will be used to check whether
            sub-models will be included in the model or not. If a sub-model
            refers to a resource which is not included in this dictionary, the
            permissions will be checked with the RBAC component.

    Returns:
        The recursively dehydrated value.
    """
    if isinstance(value, (BaseResponse, BaseResponseModel)):
        value = get_surrogate_permission_model_for_model(
            value, action=Action.READ
        )
        resource = get_resource_for_model(value)
        if not resource:
            return dehydrate_response_model(value, permissions=permissions)

        has_permissions = (permissions or {}).get(resource, False)
        if has_permissions or has_permissions_for_model(
            model=value, action=Action.READ
        ):
            return dehydrate_response_model(value, permissions=permissions)
        else:
            return get_permission_denied_model(value)
    elif isinstance(value, BaseModel):
        return dehydrate_response_model(value, permissions=permissions)
    elif isinstance(value, Dict):
        return {
            k: _dehydrate_value(v, permissions=permissions)
            for k, v in value.items()
        }
    elif isinstance(value, (List, Set, tuple)):
        type_ = type(value)
        return type_(
            _dehydrate_value(v, permissions=permissions) for v in value
        )
    else:
        return value


def has_permissions_for_model(model: AnyResponseModel, action: Action) -> bool:
    """If the active user has permissions to perform the action on the model.

    Args:
        model: The model the user wants to perform the action on.
        action: The action the user wants to perform.

    Returns:
        If the active user has permissions to perform the action on the model.
    """
    if is_owned_by_authenticated_user(model):
        return True

    try:
        verify_permission_for_model(model=model, action=action)
        return True
    except IllegalOperationError:
        return False


def get_permission_denied_model(model: AnyResponseModel) -> AnyResponseModel:
    """Get a model to return in case of missing read permissions.

    Args:
        model: The original model.

    Returns:
        The permission denied model.
    """
    if isinstance(model, BaseResponse):
        return cast(AnyResponseModel, get_permission_denied_model_v2(model))
    else:
        return cast(
            AnyResponseModel,
            get_permission_denied_model_v1(cast(BaseResponseModel, model)),
        )


def get_permission_denied_model_v2(
    model: AnyNewResponseModel,
) -> AnyNewResponseModel:
    """Get a V2 model to return in case of missing read permissions.

    This function removes the body and metadata of the model.

    Args:
        model: The original model.

    Returns:
        The model with body and metadata removed.
    """
    return model.copy(
        exclude={"body", "metadata"}, update={"permission_denied": True}
    )


def get_permission_denied_model_v1(
    model: AnyOldResponseModel, keep_id: bool = True, keep_name: bool = True
) -> AnyOldResponseModel:
    """Get a V1 model to return in case of missing read permissions.

    This function replaces all attributes except name and ID in the given model.

    Args:
        model: The original model.
        keep_id: If `True`, the model ID will not be replaced.
        keep_name: If `True`, the model name will not be replaced.

    Returns:
        The model with attribute values replaced by default values.
    """
    values = {}

    for field_name, field in model.__fields__.items():
        value = getattr(model, field_name)

        if keep_id and field_name == "id" and isinstance(value, UUID):
            pass
        elif keep_name and field_name == "name" and isinstance(value, str):
            pass
        elif field.allow_none:
            value = None
        elif isinstance(value, BaseResponseModel):
            value = get_permission_denied_model_v1(
                value, keep_id=False, keep_name=False
            )
        elif isinstance(value, BaseResponse):
            value = get_permission_denied_model_v2(value)
        elif isinstance(value, UUID):
            value = UUID(int=0)
        elif isinstance(value, datetime):
            value = datetime.utcnow()
        elif isinstance(value, Enum):
            # TODO: handle enums in a more sensible way
            value = list(type(value))[0]
        else:
            type_ = type(value)
            # For the remaining cases (dict, list, set, tuple, int, float, str),
            # simply return an empty value
            value = type_()

        values[field_name] = value

    values["missing_permissions"] = True

    return type(model).parse_obj(values)


def batch_verify_permissions_for_models(
    models: Sequence[AnyResponseModel],
    action: Action,
) -> None:
    """Batch permission verification for models.

    Args:
        models: The models the user wants to perform the action on.
        action: The action the user wants to perform.
    """
    if not server_config().rbac_enabled:
        return

    resources = set()
    for model in models:
        if is_owned_by_authenticated_user(model):
            # The model owner always has permissions
            continue

        permission_model = get_surrogate_permission_model_for_model(
            model, action=action
        )

        if resource := get_resource_for_model(permission_model):
            resources.add(resource)

    batch_verify_permissions(resources=resources, action=action)


def verify_permission_for_model(
    model: AnyResponseModel,
    action: Action,
) -> None:
    """Verifies if a user has permission to perform an action on a model.

    Args:
        model: The model the user wants to perform the action on.
        action: The action the user wants to perform.
    """
    batch_verify_permissions_for_models(models=[model], action=action)


def batch_verify_permissions(
    resources: Set[Resource],
    action: Action,
) -> None:
    """Batch permission verification.

    Args:
        resources: The resources the user wants to perform the action on.
        action: The action the user wants to perform.

    Raises:
        IllegalOperationError: If the user is not allowed to perform the action.
        RuntimeError: If the permission verification failed unexpectedly.
    """
    if not server_config().rbac_enabled:
        return

    auth_context = get_auth_context()
    assert auth_context

    permissions = rbac().check_permissions(
        user=auth_context.user, resources=resources, action=action
    )

    for resource in resources:
        if resource not in permissions:
            # This should never happen if the RBAC implementation is working
            # correctly
            raise RuntimeError(
                f"Failed to verify permissions to {action.upper()} resource "
                f"'{resource}'."
            )

        if not permissions[resource]:
            raise IllegalOperationError(
                message=f"Insufficient permissions to {action.upper()} "
                f"resource '{resource}'.",
            )


def verify_permission(
    resource_type: str,
    action: Action,
    resource_id: Optional[UUID] = None,
) -> None:
    """Verifies if a user has permission to perform an action on a resource.

    Args:
        resource_type: The type of resource that the user wants to perform the
            action on.
        action: The action the user wants to perform.
        resource_id: ID of the resource the user wants to perform the action on.
    """
    resource = Resource(type=resource_type, id=resource_id)
    batch_verify_permissions(resources={resource}, action=action)


def get_allowed_resource_ids(
    resource_type: str,
    action: Action = Action.READ,
) -> Optional[Set[UUID]]:
    """Get all resource IDs of a resource type that a user can access.

    Args:
        resource_type: The resource type.
        action: The action the user wants to perform on the resource.

    Returns:
        A list of resource IDs or `None` if the user has full access to the
        all instances of the resource.
    """
    if not server_config().rbac_enabled:
        return None

    auth_context = get_auth_context()
    assert auth_context

    (
        has_full_resource_access,
        allowed_ids,
    ) = rbac().list_allowed_resource_ids(
        user=auth_context.user,
        resource=Resource(type=resource_type),
        action=action,
    )

    if has_full_resource_access:
        return None

    return {UUID(id) for id in allowed_ids}


def get_resource_for_model(model: AnyResponseModel) -> Optional[Resource]:
    """Get the resource associated with a model object.

    Args:
        model: The model for which to get the resource.

    Returns:
        The resource associated with the model, or `None` if the model
        is not associated with any resource type.
    """
    resource_type = get_resource_type_for_model(model)
    if not resource_type:
        # This model is not tied to any RBAC resource type
        return None

    return Resource(type=resource_type, id=model.id)


def get_surrogate_permission_model_for_model(
    model: AnyResponseModel, action: str
) -> Union[BaseResponse[Any, Any], BaseResponseModel]:
    """Get a surrogate permission model for a model.

    In some cases a different model instead of the original model is used to
    verify permissions. For example, a parent container model might be used
    to verify permissions for all its children.

    Args:
        model: The original model.
        action: The action that the user wants to perform on the model.

    Returns:
        A surrogate model or the original.
    """
    from zenml.models import ModelVersionResponseModel

    if action == Action.READ and isinstance(model, ModelVersionResponseModel):
        # Permissions to read a model version is the same as reading the model
        return model.model

    return model


def get_resource_type_for_model(
    model: AnyResponseModel,
) -> Optional[ResourceType]:
    """Get the resource type associated with a model object.

    Args:
        model: The model for which to get the resource type.

    Returns:
        The resource type associated with the model, or `None` if the model
        is not associated with any resource type.
    """
    from zenml.models import (
        ArtifactResponse,
        CodeRepositoryResponse,
        ComponentResponse,
        FlavorResponse,
        ModelResponseModel,
        PipelineBuildResponse,
        PipelineDeploymentResponse,
        PipelineResponse,
        PipelineRunResponse,
        RunMetadataResponse,
        SecretResponseModel,
        ServiceAccountResponse,
        ServiceConnectorResponse,
        StackResponse,
        TagResponseModel,
        UserResponse,
        WorkspaceResponse,
    )

    mapping: Dict[
        Any,
        ResourceType,
    ] = {
        FlavorResponse: ResourceType.FLAVOR,
        ServiceConnectorResponse: ResourceType.SERVICE_CONNECTOR,
        ComponentResponse: ResourceType.STACK_COMPONENT,
        StackResponse: ResourceType.STACK,
        PipelineResponse: ResourceType.PIPELINE,
        CodeRepositoryResponse: ResourceType.CODE_REPOSITORY,
        SecretResponseModel: ResourceType.SECRET,
        ModelResponseModel: ResourceType.MODEL,
        ArtifactResponse: ResourceType.ARTIFACT,
        WorkspaceResponse: ResourceType.WORKSPACE,
        UserResponse: ResourceType.USER,
        RunMetadataResponse: ResourceType.RUN_METADATA,
        PipelineDeploymentResponse: ResourceType.PIPELINE_DEPLOYMENT,
        PipelineBuildResponse: ResourceType.PIPELINE_BUILD,
        PipelineRunResponse: ResourceType.PIPELINE_RUN,
        TagResponseModel: ResourceType.TAG,
        ServiceAccountResponse: ResourceType.SERVICE_ACCOUNT,
    }

    return mapping.get(type(model))


def is_owned_by_authenticated_user(model: AnyResponseModel) -> bool:
    """Returns whether the currently authenticated user owns the model.

    Args:
        model: The model for which to check the ownership.

    Returns:
        Whether the currently authenticated user owns the model.
    """
    auth_context = get_auth_context()
    assert auth_context

    if isinstance(model, (UserScopedResponseModel, UserScopedResponse)):
        if model.user:
            return model.user.id == auth_context.user.id
        else:
            # The model is server-owned and for RBAC purposes we consider
            # every user to be the owner of it
            return True

    return False


def get_subresources_for_model(
    model: AnyModel,
) -> Set[Resource]:
    """Get all subresources of a model which need permission verification.

    Args:
        model: The model for which to get all the resources.

    Returns:
        All resources of a model which need permission verification.
    """
    resources = set()

    for field_name in model.__fields__.keys():
        value = getattr(model, field_name)
        resources.update(_get_subresources_for_value(value))

    return resources


def _get_subresources_for_value(value: Any) -> Set[Resource]:
    """Helper function to recursive retrieve resources of any object.

    Args:
        value: The value for which to get all the resources.

    Returns:
        All resources of the value which need permission verification.
    """
    if isinstance(value, (BaseResponse, BaseResponseModel)):
        resources = set()
        if not is_owned_by_authenticated_user(value):
            value = get_surrogate_permission_model_for_model(
                value, action=Action.READ
            )
            if resource := get_resource_for_model(value):
                resources.add(resource)

        return resources.union(get_subresources_for_model(value))
    elif isinstance(value, BaseModel):
        return get_subresources_for_model(value)
    elif isinstance(value, Dict):
        resources_list = [
            _get_subresources_for_value(v) for v in value.values()
        ]
        return set.union(*resources_list) if resources_list else set()
    elif isinstance(value, (List, Set, tuple)):
        resources_list = [_get_subresources_for_value(v) for v in value]
        return set.union(*resources_list) if resources_list else set()
    else:
        return set()