<#ftl output_format="plainText">
<#import "template.ftl" as layout>
<#assign exp = linkExpirationFormatter(linkExpiration)>
<@layout.emailLayout heading=msg("emailVerificationHeading") link=link label=msg("emailVerificationButton") note=msg("emailVerificationNote", exp)>
<#if (user.firstName)?has_content>${msg("greetingName", user.firstName)}<#else>${msg("greeting")}</#if>

${msg("emailVerificationLead")}
</@layout.emailLayout>
