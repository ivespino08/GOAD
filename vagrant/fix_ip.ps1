# vmware bug to set the ip
# see : https://github.com/hashicorp/vagrant/issues/5000#issuecomment-258209286

param ([String] $ip)

$subnet = $ip -replace "\.\d+$", ""

$name = (Get-NetIPAddress -AddressFamily IPv4 `
   | Where-Object -FilterScript { ($_.IPAddress).StartsWith($subnet) } `
   ).InterfaceAlias

if ($name) {
  Write-Host "Set IP address to $ip of interface $name"
  & netsh.exe int ip set address "$name" static $ip 255.255.255.0 "$subnet.1"
}