<#ftl output_format="plainText">
<#import "template.ftl" as layout>
<@layout.emailLayout heading=msg("emailTestHeading") note=msg("emailTestNote")>
${msg("emailTestLead")}
</@layout.emailLayout>
