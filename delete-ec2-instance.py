import boto3
import time

def list_ec2_instances():
    ec2 = boto3.client('ec2')

    try:
        print("Fetching EC2 instances...")
        response = ec2.describe_instances()
        
        instances = []
        for reservation in response['Reservations']:
            for instance in reservation['Instances']:
                instance_name = "No Name"
                for tag in instance.get('Tags', []):
                    if tag['Key'] == 'Name':
                        instance_name = tag['Value']
                
                instance_state = instance['State']['Name']
                
                instances.append({
                    'InstanceName': instance_name,
                    'State': instance_state,
                    'InstanceId': instance['InstanceId']
                })
        
        if not instances:
            print("No EC2 instances found.")
            return None
        
        print("\nList of EC2 Instances:")
        for idx, instance in enumerate(instances):
            print(f"{idx + 1}. Name: {instance['InstanceName']}, State: {instance['State']}")
        
        return instances

    except Exception as e:
        print(f"Error fetching EC2 instances: {e}")
        return None

def delete_ec2_instances(instance_ids, instances):
    ec2 = boto3.client('ec2')

    try:
        print(f"\nAttempting to terminate EC2 instances: {', '.join(instance_ids)}...")
        response = ec2.terminate_instances(InstanceIds=instance_ids)

        print("\nTermination initiated for the following EC2 Instances:")
        for instance in response['TerminatingInstances']:
            instance_id = instance['InstanceId']
            current_state = instance['CurrentState']['Name']
            instance_name = next(inst['InstanceName'] for inst in instances if inst['InstanceId'] == instance_id)
            print(f"- Name: {instance_name}, State: {current_state}")

        print("\nWaiting for instances to terminate...")
        terminated_instances = []
        
        while len(terminated_instances) < len(instance_ids):
            time.sleep(10)
            
            status = ec2.describe_instances(InstanceIds=instance_ids)
            
            for reservation in status['Reservations']:
                for instance in reservation['Instances']:
                    if instance['State']['Name'] == 'terminated':
                        instance_name = next(inst['InstanceName'] for inst in instances if inst['InstanceId'] == instance['InstanceId'])
                        terminated_instances.append(instance['InstanceId'])
                        print(f"- Name: {instance_name}, State: terminated")
        
        print("\nAll selected instances have been terminated.")

    except Exception as e:
        print(f"Error terminating EC2 instances: {e}")

if __name__ == "__main__":
    instances = list_ec2_instances()
    
    if instances:
        # ✅ ONLY CHANGE MADE HERE
        user_input = input("\nDo you want to delete all EC2 instances? (yes/no, default yes): ").strip().lower() or "yes"
        
        if user_input == 'yes':
            instance_ids = [instance['InstanceId'] for instance in instances]
            delete_ec2_instances(instance_ids, instances)
        else:
            try:
                selected_instances = input("\nEnter the numbers of the EC2 instances you want to delete, separated by commas (e.g., 1, 3, 5): ")
                selected_indexes = [int(i.strip()) - 1 for i in selected_instances.split(',')]

                if all(0 <= idx < len(instances) for idx in selected_indexes):
                    instance_ids_to_delete = [instances[idx]['InstanceId'] for idx in selected_indexes]
                    print(f"\nYou selected the following instances to delete:")
                    for idx in selected_indexes:
                        print(f"- Name: {instances[idx]['InstanceName']}, State: {instances[idx]['State']}")
                    delete_ec2_instances(instance_ids_to_delete, instances)
                else:
                    print("Invalid selection. Please ensure the numbers are within the list of instances.")
            except ValueError:
                print("Invalid input. Please enter a list of numbers separated by commas.")
