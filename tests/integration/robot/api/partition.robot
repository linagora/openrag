*** Settings ***
Library     String
Library     Collections
Library     RequestsLibrary
Resource    keywords.robot

*** Variables ***
${PARTITIONS_ENDPOINT}    ${BASE_URL}/partition
${USERS_ENDPOINT}    ${BASE_URL}/users
${PARTITION_1}    ptest1
${PARTITION_2}    ptest2

*** Test Cases ***

# --- Partition Setup ---

List Partitions Initially As Admin
    ${response}=    GET    ${PARTITIONS_ENDPOINT}    headers=${HEADERS}    expected_status=200
    ${json}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${json}    partitions

Create Partition 1
    ${response}=    POST    ${PARTITIONS_ENDPOINT}/${PARTITION_1}    headers=${HEADERS}    expected_status=201

Create Partition 2
    ${response}=    POST    ${PARTITIONS_ENDPOINT}/${PARTITION_2}    headers=${HEADERS}    expected_status=201

# --- User Creation ---

Create User For Partition Tests
    ${display_name}=    Generate Random String    6
    ${external_user_id}=    Generate Random String    12
    ${user}=   Create User   ${display_name}    ${external_user_id}
    Set Suite Variable    ${USER_ID}    ${user}[id]
    Set Suite Variable    ${USER_TOKEN}    ${user}[token]

# --- Sharing Partitions ---

Add User To Partition 1
    ${form_data}=    Create Dictionary
    ...    user_id=${USER_ID}
    ...    role=editor    
    ${response}=    POST
    ...    ${PARTITIONS_ENDPOINT}/${PARTITION_1}/users
    ...    headers=${HEADERS}
    ...    data=${form_data}
    ...    expected_status=201

List Members In Partition 1
    ${response}=    GET    ${PARTITIONS_ENDPOINT}/${PARTITION_1}/users    headers=${HEADERS}    expected_status=200
    ${json}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${json}    members
    ${members}=    Get From Dictionary    ${json}    members
    Should Be True    ${members} != []
    ${user_ids}=    Create List
    FOR    ${member}    IN    @{members}
        Append To List    ${user_ids}    ${member}[user_id]
    END
    List Should Contain Value    ${user_ids}    ${USER_ID}


# --- List Partitions with Different Tokens ---

List Partitions As Admin
    ${response}=    GET    ${PARTITIONS_ENDPOINT}    headers=${HEADERS}    expected_status=200
    ${json}=    Set Variable    ${response.json()}
    ${partitions}=    Get From Dictionary    ${json}    partitions
    ${partition_names}=    Create List
    FOR    ${p}    IN    @{partitions}
        Append To List    ${partition_names}    ${p}[partition]
    END
    List Should Contain Value    ${partition_names}    ${PARTITION_1}
    List Should Contain Value    ${partition_names}    ${PARTITION_2}

List Partitions As User
    Skip If Auth Disabled
    &{user_headers}=    Create Dictionary    Authorization=Bearer ${USER_TOKEN}
    ${response}=    GET    ${PARTITIONS_ENDPOINT}    headers=${user_headers}    expected_status=200
    ${json}=    Set Variable    ${response.json()}
    ${partitions}=    Get From Dictionary    ${json}    partitions
    ${partition_names}=    Create List
    FOR    ${p}    IN    @{partitions}
        Append To List    ${partition_names}    ${p}[partition]
    END
    List Should Contain Value    ${partition_names}    ${PARTITION_1}
    List Should Not Contain Value    ${partition_names}    ${PARTITION_2}

# --- Update / Remove ---

Update User Role To Owner
    ${form_data}=    Create Dictionary  role=owner    
    ${response}=    PATCH
    ...    ${PARTITIONS_ENDPOINT}/${PARTITION_1}/users/${USER_ID}
    ...    headers=${HEADERS}
    ...    data=${form_data}
    ...    expected_status=200

Remove User From Partition 1
    ${response}=    DELETE
    ...    ${PARTITIONS_ENDPOINT}/${PARTITION_1}/users/${USER_ID}
    ...    headers=${HEADERS}
    ...    expected_status=204

# --- Cleanup ---

Cleanup
    ${response}=    DELETE    ${PARTITIONS_ENDPOINT}/${PARTITION_1}    headers=${HEADERS}    expected_status=204
    ${response}=    DELETE    ${PARTITIONS_ENDPOINT}/${PARTITION_2}    headers=${HEADERS}    expected_status=204
    ${response}=    DELETE    ${USERS_ENDPOINT}/${USER_ID}    headers=${HEADERS}    expected_status=204
