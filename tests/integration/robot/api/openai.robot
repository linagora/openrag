*** Settings ***
Resource    keywords.robot
Library     String
Library     Collections
Library     RequestsLibrary

*** Variables ***
${OPENAI_ENDPOINT}        ${BASE_URL}/v1
${MODELS_ENDPOINT}        ${OPENAI_ENDPOINT}/models
${CHAT_ENDPOINT}          ${OPENAI_ENDPOINT}/chat/completions
${COMPLETIONS_ENDPOINT}   ${OPENAI_ENDPOINT}/completions
${USERS_ENDPOINT}         ${BASE_URL}/users
${PARTITIONS_ENDPOINT}    ${BASE_URL}/partition
${PARTITION_USER}         pragu
${test_file_txt}          ../resources/test_file.txt
${QUERY}                  Tell me about OpenRag


*** Test Cases ***

# ==========================================================
# SETUP
# ==========================================================

Create User For OpenAI Tests
    ${display_name}=    Generate Random String    6
    ${external_user_id}=    Generate Random String    12
    ${user}=   Create User   ${display_name}    ${external_user_id}
    Set Suite Variable    ${USER_ID}    ${user}[id]
    Set Suite Variable    ${USER_TOKEN}    ${user}[token]
    ${user_headers}=    Create Dictionary    Authorization=Bearer ${USER_TOKEN}
    Set Suite Variable    ${USER_HEADERS}    ${user_headers}

Create A Partition For The User
    ${response}=    POST    ${PARTITIONS_ENDPOINT}/${PARTITION_USER}    headers=${USER_HEADERS}    expected_status=201


# ==========================================================
# MODEL LISTING
# ==========================================================

List Models As Admin
    [Documentation]    Admin should see all models including openrag-all and openrag-${PARTITION_USER}.
    ${response}=    GET    ${MODELS_ENDPOINT}    headers=${HEADERS}    expected_status=200
    ${json}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${json}    data
    ${models}=    Get From Dictionary    ${json}    data
    ${ids}=    Create List
    FOR    ${model}    IN    @{models}
        Append To List    ${ids}    ${model}[id]
    END
    List Should Contain Value    ${ids}    openrag-all

List Models As User
    [Documentation]    Regular user should only see openrag-all and their own openrag-${PARTITION_USER} model.
    Skip If Auth Disabled
    ${response}=    GET    ${MODELS_ENDPOINT}    headers=${USER_HEADERS}    expected_status=200
    ${json}=    Set Variable    ${response.json()}
    ${models}=    Get From Dictionary    ${json}    data
    ${ids}=    Create List
    FOR    ${model}    IN    @{models}
        Append To List    ${ids}    ${model}[id]
    END
    List Should Contain Value    ${ids}    openrag-all
    List Should Contain Value    ${ids}    openrag-${PARTITION_USER}
    Length Should Be    ${ids}    2


# ==========================================================
# INDEXING
# ==========================================================

Index Text File To User Partition
    [Documentation]    Admin indexes a text file to the user’s partition.
    Index File    ${CURDIR}/${test_file_txt}    0    ${PARTITION_USER}    ${USER_HEADERS}


# ==========================================================
# CHAT COMPLETIONS
# ==========================================================


Chat Completion As User
    [Documentation]    Regular user requests a chat completion on their partition.
    &{user_headers}=    Create Dictionary    Authorization=Bearer ${USER_TOKEN}
    &{msg}=    Create Dictionary    role=user    content=${QUERY}
    @{messages}=    Create List    ${msg}
    ${payload}=    Create Dictionary
    ...    model=openrag-${PARTITION_USER}
    ...    messages=${messages}
    ${response}=    POST
    ...    ${CHAT_ENDPOINT}
    ...    headers=${user_headers}
    ...    json=${payload}
    ...    expected_status=200
    ${json}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${json}    choices



# ==========================================================
# COMPLETIONS
# ==========================================================

Completion As User
    [Documentation]    Regular user requests a completion on their partition.
    ${payload}=    Create Dictionary
    ...    model=openrag-${PARTITION_USER}
    ...    prompt=${QUERY}
    ${response}=    POST
    ...    ${COMPLETIONS_ENDPOINT}
    ...    headers=${USER_HEADERS}
    ...    json=${payload}
    ...    expected_status=200
    ${json}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${json}    choices


# ==========================================================
# CLEANUP
# ==========================================================

Cleanup Partition
    Delete Partition    ${PARTITION_USER}    ${USER_HEADERS}
Cleanup User
    [Documentation]    Delete the user created for the tests.
    ${response}=    DELETE    ${USERS_ENDPOINT}/${USER_ID}    headers=${HEADERS}    expected_status=204
