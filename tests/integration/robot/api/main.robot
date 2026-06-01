*** Settings ***
Resource    keywords.robot

*** Test Cases ***
Health Check
    ${response}=    GET    ${BASE_URL}/health_check  headers=${HEADERS}  expected_status=any
    IF    '${response.status_code}' != '200'
        Fatal Error    Health check failed with status code: ${response.status_code}. Canceling all tests.
    END
    Should Be Equal As Strings    ${response.json()}    RAG API is up.
    IF    '${response.json()}' != 'RAG API is up.'
        Fatal Error    Health check failed: ${response.json()}. Canceling all tests.
    END