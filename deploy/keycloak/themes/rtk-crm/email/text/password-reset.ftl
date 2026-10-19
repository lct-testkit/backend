<#ftl output_format="plainText">
<#import "template.ftl" as layout>
<#assign exp = linkExpirationFormatter(linkExpiration)>
<@layout.emailLayout heading=msg("passwordResetHeading") link=link label=msg("passwordResetButton") note=msg("passwordResetNote", exp)>
<#if (user.firstName)?has_content>${msg("greetingName", user.firstName)}<#else>${msg("greeting")}</#if>

${msg("passwordResetLead")}
</@layout.emailLayout>
